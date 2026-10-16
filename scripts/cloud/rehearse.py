"""Prepare, verify and remove a dedicated Docker Desktop owner release; no AWS."""

import argparse
import base64
import hashlib
import json
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import yaml
from fastapi import HTTPException
from kubernetes import client

from backend.app.iris_target import RuntimeTarget
from backend.app.local_cluster import connect_local_cluster
from backend.app.rehearsal_cluster import connect_rehearsal
from scripts.monitoring.observations import observe
from scripts.optimization.execution import save_evidence

from .aws_commands import run_checked
from .cli import run as run_cli
from .deploy import resolve_runtime_digests
from .images import inspect_local, source_sha
from .runtime_contract import LocalImages, RuntimeReceipt
from .runtime_forwards import managed_forwards
from .runtime_install import install_runtime, remove_runtime, verify_runtime
from .runtime_manifests import render_runtime

ROOT = Path(__file__).resolve().parents[2]


def run(argv, **kwargs):
    return run_cli(argv, runner=run_checked, **kwargs)


def import_images(node, images):
    """Pipe bytes directly between child processes, including under PowerShell/WSL."""
    docker = shutil.which("docker.exe") or shutil.which("docker")
    if node != "desktop-control-plane" or not docker:
        raise RuntimeError("Only the verified Docker Desktop kind node is supported.")
    with tempfile.TemporaryFile() as errors:
        producer = subprocess.Popen(
            [
                docker,
                "image",
                "save",
                images.model_reference,
                images.operator_reference,
            ],
            stdout=subprocess.PIPE,
            stderr=errors,
            shell=False,
        )
        try:
            imported = subprocess.run(
                [
                    docker,
                    "exec",
                    "-i",
                    node,
                    "ctr",
                    "-n",
                    "k8s.io",
                    "images",
                    "import",
                    "--digests",
                    "-",
                ],
                stdin=producer.stdout,
                stdout=subprocess.DEVNULL,
                stderr=errors,
                check=False,
                timeout=180,
            )
            producer.stdout.close()
            if producer.wait(timeout=30) != 0 or imported.returncode != 0:
                raise RuntimeError("Local image import did not complete.")
        finally:
            if producer.poll() is None:
                producer.kill()
                producer.wait(timeout=5)
    for reference, identity in (
        (images.model_reference, images.model_image_id),
        (images.operator_reference, images.operator_image_id),
    ):
        verify_imported(node, reference, identity)


def verify_imported(node, reference, identity):
    qualified = (
        reference
        if reference.startswith("docker.io/")
        else "docker.io/library/" + reference
    )
    table = run(
        [
            "docker",
            "exec",
            node,
            "ctr",
            "-n",
            "k8s.io",
            "images",
            "ls",
            "name==" + qualified,
        ]
    ).splitlines()
    rows = [line.split() for line in table[1:] if line.strip()]
    if len(rows) != 1 or rows[0][0] != qualified or len(rows[0]) < 3:
        raise RuntimeError("Imported node tag is missing or ambiguous.")
    digest = rows[0][2]
    manifest, config = resolve_runtime_digests(
        reference,
        digest,
        lambda ref: run(
            [
                "docker",
                "exec",
                node,
                "ctr",
                "-n",
                "k8s.io",
                "content",
                "get",
                ref.rsplit("@", 1)[1],
            ]
        ),
    )
    actual = json.loads(run(["docker", "exec", node, "crictl", "inspecti", reference]))
    if identity not in (digest, manifest, config) or actual["status"]["id"] != config:
        raise RuntimeError(
            "Imported node image differs from its verified Docker content."
        )


def prepare(path, kubeconfig):
    path, kubeconfig = Path(path), Path(kubeconfig).resolve()
    if not path.exists():
        sha = source_sha(SimpleNamespace(run=run))
        with connect_local_cluster(kubeconfig=str(kubeconfig)) as local:
            core = client.CoreV1Api(local.sdk)
            namespaces = {
                n.metadata.name for n in core.list_namespace(_request_timeout=5).items
            }
            if namespaces.intersection({"strawhats", "strawhats-monitoring"}):
                raise RuntimeError("A foreign rehearsal namespace blocks creation.")
            nodes = core.list_node(_request_timeout=5).items
            if len(nodes) != 1 or nodes[0].metadata.name != "desktop-control-plane":
                raise RuntimeError("Expected the single Docker Desktop kind node.")
            node = json.loads(run(["docker", "inspect", "desktop-control-plane"]))[0]
            labels = node["Config"].get("Labels", {})
            if (
                not node["State"]["Running"]
                or labels.get("io.x-k8s.kind.cluster") != "desktop"
            ):
                raise RuntimeError("The Docker node does not match this local cluster.")
            baseline = {
                "namespace_uids": {
                    n.metadata.name: n.metadata.uid
                    for n in core.list_namespace(_request_timeout=5).items
                    if n.metadata.name
                    in ("strawhats-operator-local", "strawhats-monitoring-local")
                },
                "pod_uids": {
                    p.metadata.name: p.metadata.uid
                    for ns in ("strawhats-operator-local", "strawhats-monitoring-local")
                    for p in core.list_namespaced_pod(ns, _request_timeout=5).items
                },
            }
            save_evidence(path.parent / "original-local.json", baseline)
        model, operator = (
            f"strawhats-iris:rehearsal-{sha}",
            f"strawhats-model-operator:rehearsal-{sha}",
        )
        for reference, file in (
            (model, "Dockerfile.model"),
            (operator, "Dockerfile.operator"),
        ):
            run(
                [
                    "docker",
                    "build",
                    "--platform",
                    "linux/amd64",
                    "-f",
                    f"backend/{file}",
                    "-t",
                    reference,
                    ".",
                ],
                timeout_seconds=600,
            )
        images = LocalImages(
            sha,
            model,
            inspect_local(model, run),
            operator,
            inspect_local(operator, run, operator=True),
        )
        if source_sha(SimpleNamespace(run=run)) != sha:
            raise RuntimeError("The source changed during local builds.")
        import_images("desktop-control-plane", images)
        target = RuntimeTarget(
            "rehearsal",
            "docker-desktop",
            "strawhats",
            "strawhats-monitoring",
            "iris",
            model,
            "Never",
            "strawhats-model-operator",
            operator,
        )
        config = yaml.safe_load(kubeconfig.read_text(encoding="utf-8-sig"))
        context = next(
            c["context"] for c in config["contexts"] if c["name"] == "docker-desktop"
        )
        cluster = next(
            c["cluster"] for c in config["clusters"] if c["name"] == context["cluster"]
        )
        ca = base64.b64decode(cluster["certificate-authority-data"], validate=True)
        receipt = RuntimeReceipt(
            "rehearsal",
            "prepared",
            str(uuid4()),
            sha,
            target,
            images,
            str(kubeconfig),
            cluster["server"],
            hashlib.sha256(ca).hexdigest(),
        )
        receipt.save(path)
    receipt = RuntimeReceipt.load(path)
    if receipt.kind != "rehearsal":
        raise RuntimeError("Only a local rehearsal receipt is permitted.")
    with connect_rehearsal(path, purpose="install") as connection:
        installed = install_runtime(
            connection, render_runtime(connection.target), evidence_dir=path.parent
        )
    return {
        "kind": "rehearsal",
        "phase": installed.phase,
        "ready": False,
        "receipt": str(path),
    }


def verify(path):
    with connect_rehearsal(path) as connection:
        result = verify_runtime(connection, connection.receipt)
        with managed_forwards(connection) as owned:
            with httpx.Client(
                base_url="http://127.0.0.1:9090",
                trust_env=False,
                follow_redirects=False,
                timeout=5,
                event_hooks={"request": [lambda request: owned.check()]},
            ) as prometheus:
                # A new Prometheus needs enough real scrapes for rate/source rules.
                deadline = time.monotonic() + 90
                while True:
                    observation = observe(
                        connection.cluster, prometheus, target=connection.target
                    )
                    if observation.available:
                        break
                    if time.monotonic() >= deadline:
                        raise RuntimeError(
                            "Rehearsal telemetry did not become available."
                        )
                    time.sleep(2)
            with httpx.Client(
                base_url="http://127.0.0.1:3000",
                trust_env=False,
                follow_redirects=False,
                timeout=5,
            ) as grafana:
                owned.check()
                health = grafana.get("/api/health")
                dashboard = grafana.get("/api/dashboards/uid/strawhats-owner-iris")
                if (
                    health.status_code != 200
                    or health.json().get("database") != "ok"
                    or dashboard.status_code != 200
                    or "Local rehearsal" not in dashboard.json()["dashboard"]["title"]
                ):
                    raise RuntimeError(
                        "The rehearsal Grafana dashboard did not verify."
                    )
            result.update(observation=asdict(observation), grafana_dashboard=True)
        save_evidence(Path(path).parent / "verification.json", result)
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "verify", "cleanup"))
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--kubeconfig", type=Path, default=Path.home() / ".kube/config")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare(args.receipt, args.kubeconfig)
        else:
            if RuntimeReceipt.load(args.receipt).kind != "rehearsal":
                raise RuntimeError("No cloud receipt is accepted by local rehearsal.")
            if args.command == "verify":
                result = verify(args.receipt)
            else:
                with connect_rehearsal(args.receipt, purpose="cleanup") as connection:
                    result = remove_runtime(connection, connection.receipt)
        print(json.dumps(result, allow_nan=False))
        return 0
    except (
        HTTPException,
        RuntimeError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
    ):
        print(
            "Local rehearsal stopped. Inspect its saved receipt; no AWS operation or blind cleanup was attempted."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
