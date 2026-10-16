"""Local rehearsal retains precise ownership and never borrows an existing port."""

import importlib
import json
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest

from backend.tests.test_cloud_runtime import (  # noqa: F401
    connection as connection,
)
from backend.tests.test_cloud_runtime import (
    rehearsal as rehearsal,
)
from backend.tests.test_cloud_runtime import (
    runtime as runtime,
)
from backend.tests.test_cloud_runtime import (
    targets as targets,
)


@pytest.fixture
def command():
    try:
        return importlib.import_module("scripts.cloud.rehearse")
    except ModuleNotFoundError:
        pytest.fail("Local release rehearsal is not implemented")


def test_cleanup_rejects_cloud_receipt_without_connecting(
    command, monkeypatch, tmp_path
):
    monkeypatch.setattr(
        command.RuntimeReceipt, "load", lambda p: SimpleNamespace(kind="eks")
    )
    monkeypatch.setattr(
        command,
        "connect_rehearsal",
        lambda *a, **k: pytest.fail("Cloud receipt connected"),
    )
    assert command.main(["cleanup", "--receipt", str(tmp_path / "receipt.json")]) == 1


@pytest.mark.parametrize("fault", ["foreign", "uid", "borrowed", "partial"])
def test_cleanup_retains_foreign_and_borrowed_objects(
    command, runtime, connection, tmp_path, monkeypatch, fault
):
    bundle = runtime.manifests.render_runtime(connection.target)
    if fault == "borrowed":
        for doc in (*bundle.metrics_documents, *bundle.documents):
            if (
                doc in bundle.metrics_documents
                or doc["kind"] == "CustomResourceDefinition"
            ):
                actual = deepcopy(doc)
                actual["metadata"]["uid"] = "borrowed-" + str(len(connection.kube.data))
                if doc["kind"] == "Deployment":
                    actual["status"] = {
                        "observedGeneration": 1,
                        "readyReplicas": 1,
                        "availableReplicas": 1,
                        "updatedReplicas": 1,
                    }
                connection.kube.data[runtime.manifests.document_key(doc)] = actual
    if fault == "partial":
        connection.kube.fail_create = (
            "v1",
            "ServiceAccount",
            "strawhats",
            "strawhats-model-operator",
        )
        with pytest.raises(RuntimeError):
            runtime.install.install_runtime(connection, bundle, evidence_dir=tmp_path)
    else:
        runtime.install.install_runtime(connection, bundle, evidence_dir=tmp_path)
    if fault == "foreign":
        connection.kube.foreign_children = [
            {
                "kind": "ConfigMap",
                "metadata": {
                    "name": "foreign",
                    "namespace": "strawhats",
                    "uid": "foreign",
                },
            }
        ]
    if fault == "uid":
        record = connection.receipt.inventory[-1]
        connection.kube.data[runtime.manifests.document_key(record.document())][
            "metadata"
        ]["uid"] = "replacement"

    @contextmanager
    def connect(path, *, purpose):
        assert purpose == "cleanup"
        connection.purpose = purpose
        yield connection

    monkeypatch.setattr(command, "connect_rehearsal", connect)
    result = command.main(["cleanup", "--receipt", str(connection.receipt_path)])
    assert result == (1 if fault in ("foreign", "uid") else 0)
    if result:
        assert connection.kube.deleted == []
    assert all(r.ownership == "created" for r in connection.kube.deleted)
    if fault == "borrowed":
        assert connection.kube.data and all(
            v["metadata"]["uid"].startswith("borrowed-")
            for v in connection.kube.data.values()
        )


@pytest.fixture
def forwards():
    try:
        return importlib.import_module("scripts.cloud.runtime_forwards")
    except ModuleNotFoundError:
        pytest.fail("Owned runtime forwards are not implemented")


def test_busy_port_never_spawns_or_stops_another_process(forwards, monkeypatch):
    def occupied(port):
        raise RuntimeError("occupied")

    monkeypatch.setattr(forwards, "require_free_port", occupied)
    monkeypatch.setattr(
        forwards.subprocess,
        "Popen",
        lambda *a, **k: pytest.fail("Must not start with an occupied port"),
    )
    with pytest.raises(RuntimeError, match="occupied"):
        with forwards.managed_forwards(
            SimpleNamespace(
                receipt=SimpleNamespace(kind="rehearsal", phase="installed")
            )
        ):
            pytest.fail("Occupied forward accepted")


def test_wsl_uses_working_windows_docker_bridge(command, monkeypatch):
    monkeypatch.setattr(command.shutil, "which", lambda n: "/mnt/c/bin/" + n)
    calls = []
    monkeypatch.setattr(
        command, "run_checked", lambda argv, **kwargs: calls.append(argv) or "ok"
    )
    assert command.run(["docker", "inspect", "desktop-control-plane"]) == "ok"
    assert calls[0][0] == "/mnt/c/bin/docker.exe"


def test_windows_kubectl_receives_its_native_path(forwards):
    assert (
        forwards.cli_path("/mnt/c/Users/Great/.kube/config", "/mnt/c/bin/kubectl.exe")
        == "C:/Users/Great/.kube/config"
    )
    assert (
        forwards.cli_path("/home/owner/config", "/usr/bin/kubectl")
        == "/home/owner/config"
    )
    with pytest.raises(ValueError):
        forwards.cli_path("/tmp/config", "/mnt/c/bin/kubectl.exe")


@pytest.mark.parametrize("foreign", [False, True])
def test_imported_oci_index_is_bound_to_the_runtime_config(
    command, monkeypatch, foreign
):
    index, manifest, config = ("sha256:" + c * 64 for c in ("a", "b", "c"))

    def run(argv, **kwargs):
        if "ls" in argv:
            return "REF TYPE DIGEST\ndocker.io/library/image:tag index " + index + "\n"
        if "content" in argv:
            return json.dumps(
                {
                    "manifests": [
                        {
                            "platform": {"os": "linux", "architecture": "amd64"},
                            "digest": manifest,
                        }
                    ]
                }
                if argv[-1] == index
                else {"config": {"digest": config}}
            )
        if "inspecti" in argv:
            return json.dumps({"status": {"id": config}})
        pytest.fail("Unexpected import check")

    monkeypatch.setattr(command, "run", run)
    if foreign:
        with pytest.raises(RuntimeError):
            command.verify_imported(
                "desktop-control-plane", "image:tag", "sha256:" + "d" * 64
            )
    else:
        command.verify_imported("desktop-control-plane", "image:tag", index)


@pytest.mark.parametrize("crashed", [False, True])
def test_only_owned_forwards_are_stopped_and_crashes_reject_reads(
    forwards, connection, monkeypatch, crashed
):
    connection.purpose = "operate"
    connection.receipt = SimpleNamespace(
        kind="rehearsal", phase="installed", kubeconfig="local-config"
    )
    monkeypatch.setattr(forwards, "require_free_port", lambda p: None)
    monkeypatch.setattr(forwards, "wait_ready", lambda *a: None)
    monkeypatch.setattr(forwards.shutil, "which", lambda n: n)
    processes = []

    class Process:
        def __init__(self, argv, **kwargs):
            assert "--address" in argv and argv[-1] == "127.0.0.1"
            assert "docker-desktop" in argv and "strawhats-monitoring" in argv
            self.returncode, self.stopped = None, False
            processes.append(self)

        def poll(self):
            return self.returncode

        def terminate(self):
            self.stopped = True
            self.returncode = 0

        def wait(self, **kwargs):
            return self.returncode

    monkeypatch.setattr(forwards.subprocess, "Popen", Process)
    with forwards.managed_forwards(connection) as owned:
        assert len(processes) == 2
        if crashed:
            processes[0].returncode = 1
            with pytest.raises(RuntimeError):
                owned.check()
        else:
            owned.check()
    assert all(p.stopped or p.returncode == 1 for p in processes)
