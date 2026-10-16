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


def test_cleanup_discovery_lists_real_resources_once_and_skips_sdk_list_wrappers(
    runtime,
):
    from kubernetes.dynamic.resource import ResourceList

    calls = []
    child = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "owned", "namespace": "strawhats", "uid": "owned-pod"},
    }

    def get(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(to_dict=lambda: {"items": [child]})

    actual = SimpleNamespace(name="pods", kind="Pod", verbs=["list"], group="", get=get)
    wrapper = ResourceList(
        SimpleNamespace(resources=SimpleNamespace(get=lambda **kwargs: actual)),
        base_kind="Pod",
    )
    objects = object.__new__(runtime.install.RuntimeObjects)
    objects.dynamic = SimpleNamespace(
        resources=SimpleNamespace(search=lambda **kwargs: [wrapper, actual])
    )
    assert objects.children("strawhats") == [child]
    assert calls == [{"namespace": "strawhats", "_request_timeout": 5}]


@pytest.mark.parametrize("foreign", [False, True])
def test_cleanup_recognizes_only_legacy_endpoints_for_owned_service_pods(
    runtime, connection, tmp_path, foreign
):
    receipt = runtime.install.install_runtime(
        connection,
        runtime.manifests.render_runtime(connection.target),
        evidence_dir=tmp_path,
    )
    deployment = next(
        r
        for r in receipt.inventory
        if r.kind == "Deployment" and r.name == "prometheus"
    )
    pod = {
        "kind": "Pod",
        "metadata": {
            "namespace": "strawhats-monitoring",
            "name": "prometheus-pod",
            "uid": "owned-pod",
            "labels": {"app": "strawhats-prometheus"},
            "ownerReferences": [{"uid": deployment.uid, "controller": True}],
        },
    }
    endpoints = {
        "kind": "Endpoints",
        "metadata": {
            "namespace": "strawhats-monitoring",
            "name": "prometheus",
            "uid": "automatic-endpoints",
            "labels": {"endpoints.kubernetes.io/managed-by": "endpoint-controller"},
        },
        "subsets": [
            {
                "addresses": [
                    {
                        "ip": "10.0.0.1",
                        "targetRef": {
                            "kind": "Pod",
                            "namespace": "strawhats-monitoring",
                            "name": "prometheus-pod",
                            "uid": "foreign-pod" if foreign else "owned-pod",
                        },
                    }
                ]
            }
        ],
    }
    connection.kube.foreign_children = [pod, endpoints]
    delete = connection.kube.delete

    def delete_with_service_gc(record):
        delete(record)
        if record.kind == "Service":
            connection.kube.foreign_children = [
                d
                for d in connection.kube.foreign_children
                if not (
                    d["kind"] == "Endpoints"
                    and d["metadata"]["namespace"] == record.namespace
                    and d["metadata"]["name"] == record.name
                )
            ]

    connection.kube.delete = delete_with_service_gc
    connection.purpose = "cleanup"
    if foreign:
        with pytest.raises(RuntimeError, match="foreign"):
            runtime.install.remove_runtime(connection, receipt)
        assert connection.kube.deleted == []
    else:
        assert runtime.install.remove_runtime(connection, receipt)["removed"]


@pytest.mark.parametrize("system", [False, True])
def test_cleanup_after_restart_handles_only_system_events_for_deleted_pods(
    runtime, connection, tmp_path, system
):
    receipt = runtime.install.install_runtime(
        connection,
        runtime.manifests.render_runtime(connection.target),
        evidence_dir=tmp_path,
    )
    connection.kube.foreign_children = [
        {
            "kind": "Event",
            "metadata": {
                "name": "old-pod.started",
                "namespace": "strawhats",
                "uid": "event-uid",
            },
            "reportingComponent": "kubelet" if system else "foreign-producer",
            "involvedObject": {
                "kind": "Pod",
                "namespace": "strawhats",
                "name": "old-pod",
                "uid": "deleted-pod-uid",
            },
        }
    ]
    connection.purpose = "cleanup"
    if system:
        assert runtime.install.remove_runtime(connection, receipt)["removed"]
    else:
        with pytest.raises(RuntimeError, match="foreign"):
            runtime.install.remove_runtime(connection, receipt)
        assert connection.kube.deleted == []


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
