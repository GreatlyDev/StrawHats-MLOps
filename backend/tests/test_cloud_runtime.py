"""Rendered runtime and UID-bound ownership behavior without a real cluster."""

import importlib
import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.tests.test_iris_targets import rehearsal as rehearsal
from backend.tests.test_iris_targets import targets as targets
from backend.tests.test_optimization_execution import cluster as cluster
from backend.tests.test_optimization_execution import harness as harness
from backend.tests.test_optimization_execution import monitoring as monitoring
from backend.tests.test_optimization_execution import responses as responses


@pytest.fixture
def runtime():
    try:
        return SimpleNamespace(
            contract=importlib.import_module("scripts.cloud.runtime_contract"),
            manifests=importlib.import_module("scripts.cloud.runtime_manifests"),
            install=importlib.import_module("scripts.cloud.runtime_install"),
        )
    except ModuleNotFoundError:
        pytest.fail("Owned runtime preparation is not implemented")


def key(doc):
    m = doc["metadata"]
    return doc["apiVersion"], doc["kind"], m.get("namespace", ""), m["name"]


@pytest.fixture
def connection(runtime, rehearsal, tmp_path):
    contract = runtime.contract
    images = contract.LocalImages(
        "39f81d1" + "a" * 33,
        rehearsal.image,
        "sha256:" + "1" * 64,
        rehearsal.operator_image,
        "sha256:" + "2" * 64,
    )
    receipt = contract.RuntimeReceipt(
        kind="rehearsal",
        phase="prepared",
        release_id=str(uuid4()),
        source_sha="39f81d1" + "a" * 33,
        target=rehearsal,
        images=images,
        kubeconfig=str(tmp_path / "kubeconfig"),
        endpoint="https://127.0.0.1:60045",
        ca_sha256="c" * 64,
    )

    class Objects:
        def __init__(self):
            self.data, self.writes, self.deleted = {}, [], []
            self.fail_create = None
            self.free = (2000, 4096)
            self.foreign_children = []

        def get(self, doc):
            return deepcopy(self.data.get(key(doc)))

        def create(self, doc, *, dry_run=False):
            if self.fail_create == key(doc):
                raise RuntimeError("Rejected by the API")
            result = deepcopy(doc)
            result["metadata"]["uid"] = str(uuid4())
            if not dry_run:
                if key(doc) in self.data:
                    raise RuntimeError("Already exists")
                self.writes.append(key(doc))
                self.data[key(doc)] = result
            return result

        def delete(self, record):
            doc = record.document()
            current = self.data.get(key(doc))
            if current and current["metadata"]["uid"] != record.uid:
                raise RuntimeError("UID changed")
            self.deleted.append(record)
            self.data.pop(key(doc), None)

        def available_capacity(self):
            return self.free

        def children(self, namespace):
            return [
                d
                for d in self.data.values()
                if d["metadata"].get("namespace") == namespace
            ] + self.foreign_children

    value = SimpleNamespace(
        target=rehearsal,
        receipt=receipt,
        receipt_path=tmp_path / "receipt.json",
        purpose="install",
        checks=0,
        kube=Objects(),
    )

    def guard():
        value.checks += 1

    value.check_mutation = guard
    return value


def test_render_uses_operator_cr_and_rebinds_monitoring(runtime, rehearsal):
    bundle = runtime.manifests.render_runtime(rehearsal)
    docs = bundle.documents
    assert not [
        d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "iris"
    ]
    models = [d for d in docs if d["kind"] == "ModelDeployment"]
    assert len(models) == 1 and models[0]["spec"]["replicas"] == 1
    assert models[0]["spec"]["image"] == "strawhats-iris:rehearsal-39f81d1"
    assert models[0]["metadata"]["namespace"] == "strawhats"
    configs = [d for d in docs if d["kind"] == "ConfigMap"]
    prometheus = next(
        d for d in configs if d["metadata"]["name"] == "prometheus-config"
    )
    assert "strawhats-operator-local" not in prometheus["data"]["prometheus.yaml"]
    assert "strawhats" in prometheus["data"]["prometheus.yaml"]
    assert not any(d["kind"] == "Secret" for d in docs)
    assert {
        d["spec"].get("type", "ClusterIP") for d in docs if d["kind"] == "Service"
    } == {"ClusterIP"}


def test_cloud_render_never_inherits_local_metrics_tls_exception(
    runtime, targets, rehearsal
):
    registry = "123456789012.dkr.ecr.us-east-1.amazonaws.com/strawhats-midterm/"
    eks = replace(
        rehearsal,
        kind="eks",
        context="arn:aws:eks:us-east-1:123456789012:cluster/strawhats-midterm",
        image=registry + "iris@sha256:" + "a" * 64,
        operator_image=registry + "operator@sha256:" + "b" * 64,
        image_pull_policy="IfNotPresent",
    )
    bundle = runtime.manifests.render_runtime(eks)
    metrics = next(d for d in bundle.metrics_documents if d["kind"] == "Deployment")
    assert (
        "--kubelet-insecure-tls"
        not in metrics["spec"]["template"]["spec"]["containers"][0]["args"]
    )
    controller = next(
        d
        for d in bundle.documents
        if d["kind"] == "Deployment"
        and d["metadata"]["name"] == "strawhats-model-operator"
    )
    assert (
        controller["spec"]["template"]["spec"]["containers"][0]["image"]
        == eks.operator_image
    )


def test_install_records_every_uid_but_does_not_claim_ready(
    runtime, connection, tmp_path
):
    receipt = runtime.install.install_runtime(
        connection,
        runtime.manifests.render_runtime(connection.target),
        evidence_dir=tmp_path,
    )
    assert receipt.phase == "installed"
    assert len(receipt.inventory) == len(connection.kube.writes)
    assert all(r.uid for r in receipt.inventory)
    assert connection.checks >= len(connection.kube.writes)
    restored = runtime.contract.RuntimeReceipt.load(connection.receipt_path)
    assert (
        restored.release_id == receipt.release_id
        and restored.inventory == receipt.inventory
    )


@pytest.mark.parametrize(
    "kind,name,namespace",
    [
        ("Namespace", "strawhats", ""),
        ("Deployment", "iris", "strawhats"),
        ("Service", "iris", "strawhats"),
        ("CustomResourceDefinition", "modeldeployments.mlops.strawhats.dev", ""),
    ],
)
def test_foreign_collision_stops_before_any_write(
    runtime, connection, tmp_path, kind, name, namespace
):
    doc = {
        "apiVersion": "apps/v1"
        if kind == "Deployment"
        else "apiextensions.k8s.io/v1"
        if kind == "CustomResourceDefinition"
        else "v1",
        "kind": kind,
        "metadata": {"name": name, "namespace": namespace, "uid": "foreign"},
        "spec": {},
    }
    connection.kube.data[key(doc)] = doc
    with pytest.raises(RuntimeError):
        runtime.install.install_runtime(
            connection,
            runtime.manifests.render_runtime(connection.target),
            evidence_dir=tmp_path,
        )
    assert connection.kube.writes == []


def test_insufficient_capacity_never_adds_a_worker(runtime, connection, tmp_path):
    connection.kube.free = (100, 128)
    with pytest.raises(RuntimeError, match="capacity"):
        runtime.install.install_runtime(
            connection,
            runtime.manifests.render_runtime(connection.target),
            evidence_dir=tmp_path,
        )
    assert connection.kube.writes == []


def test_partial_install_retains_uid_inventory_and_can_remove(
    runtime, connection, tmp_path
):
    connection.kube.fail_create = (
        "v1",
        "ServiceAccount",
        "strawhats",
        "strawhats-model-operator",
    )
    with pytest.raises(RuntimeError):
        runtime.install.install_runtime(
            connection,
            runtime.manifests.render_runtime(connection.target),
            evidence_dir=tmp_path,
        )
    receipt = runtime.contract.RuntimeReceipt.load(connection.receipt_path)
    assert receipt.phase == "partial" and receipt.inventory
    connection.purpose = "cleanup"
    result = runtime.install.remove_runtime(connection, receipt)
    assert result["removed"] is True and connection.kube.data == {}


def test_matching_shared_crd_is_borrowed_and_never_deleted(
    runtime, connection, tmp_path
):
    bundle = runtime.manifests.render_runtime(connection.target)
    crd = deepcopy(
        next(d for d in bundle.documents if d["kind"] == "CustomResourceDefinition")
    )
    crd["metadata"]["uid"] = "borrowed-crd"
    connection.kube.data[key(crd)] = crd
    receipt = runtime.install.install_runtime(connection, bundle, evidence_dir=tmp_path)
    connection.purpose = "cleanup"
    runtime.install.remove_runtime(connection, receipt)
    assert connection.kube.get(crd)["metadata"]["uid"] == "borrowed-crd"
    assert all(r.uid != "borrowed-crd" for r in connection.kube.deleted)


def test_foreign_child_or_recreated_uid_blocks_cleanup(runtime, connection, tmp_path):
    receipt = runtime.install.install_runtime(
        connection,
        runtime.manifests.render_runtime(connection.target),
        evidence_dir=tmp_path,
    )
    connection.purpose = "cleanup"
    connection.kube.foreign_children = [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "foreign", "namespace": "strawhats", "uid": "foreign"},
        }
    ]
    with pytest.raises(RuntimeError, match="foreign"):
        runtime.install.remove_runtime(connection, receipt)
    assert connection.kube.deleted == []

    connection.kube.foreign_children = []
    record = receipt.inventory[-1]
    connection.kube.data[key(record.document())]["metadata"]["uid"] = "recreated"
    with pytest.raises(RuntimeError, match="UID"):
        runtime.install.remove_runtime(connection, receipt)
    assert connection.kube.deleted == []


def test_legacy_apply_refuses_operator_owned_iris_before_apply(tmp_path):
    from scripts.cloud import deploy

    receipt = tmp_path / "image.json"
    repository = "123456789012.dkr.ecr.us-east-1.amazonaws.com/strawhats-midterm/iris"
    receipt.write_text(
        json.dumps(
            {"account": "123456789012", "image": repository + "@sha256:" + "a" * 64}
        )
    )
    calls = []

    def kubectl(*argv, **kwargs):
        calls.append(argv)
        if "get" in argv:
            return json.dumps(
                {
                    "metadata": {
                        "ownerReferences": [
                            {
                                "kind": "ModelDeployment",
                                "controller": True,
                                "uid": "owner",
                            }
                        ]
                    }
                }
            )
        pytest.fail("Legacy apply must not overwrite operator objects")

    cloud = SimpleNamespace(
        stack=lambda: (
            {},
            {"RepositoryUri": repository, "RepositoryName": "strawhats-midterm/iris"},
        ),
        args=SimpleNamespace(account="123456789012", image_receipt=receipt),
        aws=lambda *a, **k: {},
        kubectl=kubectl,
    )
    with pytest.raises(RuntimeError, match="operator"):
        deploy.apply(cloud)
    assert not any("apply" in argv for argv in calls)


def test_owner_release_help_is_offline(monkeypatch):
    try:
        module = importlib.import_module("scripts.cloud.owner_release")
    except ModuleNotFoundError:
        pytest.fail("Owner runtime release CLI is not implemented")
    from scripts.cloud import deploy

    monkeypatch.setattr(
        deploy, "Cloud", lambda *a, **k: pytest.fail("--help must not contact AWS")
    )
    with pytest.raises(SystemExit) as result:
        module.main(["--help"])
    assert result.value.code == 0


@pytest.mark.parametrize("fault", [None, "pod_image", "endpoints", "resource_uid"])
def test_acceptance_requires_exact_owned_pods_endpoints_and_service(
    runtime, connection, tmp_path, harness, fault
):
    from backend.tests.test_iris_targets import bind

    bind(harness, connection.target)
    receipt = runtime.install.install_runtime(
        connection,
        runtime.manifests.render_runtime(connection.target),
        evidence_dir=tmp_path,
    )
    for doc in connection.kube.data.values():
        if doc["kind"] == "Deployment":
            doc["metadata"]["generation"] = 1
            doc["status"] = {
                "observedGeneration": 1,
                "readyReplicas": 1,
                "updatedReplicas": 1,
                "availableReplicas": 1,
            }
    model = next(r for r in receipt.inventory if r.kind == "ModelDeployment")
    # The SDK resource, pods and Service must all belong to the captured CR UID.
    harness.cluster.data["metadata"]["uid"] = model.uid
    for pod in harness.pods.items:
        pod.metadata.labels["mlops.strawhats.dev/owner-uid"] = model.uid
    if fault == "pod_image":
        harness.pods.items[0].spec.containers[0].image = "foreign:latest"
    elif fault == "endpoints":
        harness.endpoints = False
    elif fault == "resource_uid":
        harness.cluster.data["metadata"]["uid"] = "recreated"
    connection.cluster = harness.cluster
    connection.purpose = "operate"
    if fault:
        with pytest.raises((RuntimeError, ValueError)):
            runtime.install.verify_runtime(
                connection,
                receipt,
                monotonic=harness.clock.monotonic,
                wait=harness.clock.sleep,
            )
    else:
        result = runtime.install.verify_runtime(
            connection,
            receipt,
            monotonic=harness.clock.monotonic,
            wait=harness.clock.sleep,
        )
        assert result["ready"] is True and result["prediction"]["label"] == "setosa"


def test_prepared_releases_preserve_their_own_kubeconfig(monkeypatch, tmp_path):
    import base64
    from datetime import datetime, timedelta, timezone

    from scripts.cloud import owner_release
    from scripts.cloud.images import ReleaseImage, ReleaseImages

    account = "123456789012"
    registry = f"{account}.dkr.ecr.us-east-1.amazonaws.com/strawhats-midterm/"
    sha = "a" * 40

    def image(name, letter):
        return ReleaseImage(
            registry + name,
            registry + name + "@sha256:" + letter * 64,
            "sha256:" + letter * 64,
            "sha256:" + "c" * 64,
        )

    images = ReleaseImages(sha, account, 50, image("iris", "a"), image("operator", "b"))
    context = f"arn:aws:eks:us-east-1:{account}:cluster/strawhats-midterm"
    owner = f"arn:aws:iam::{account}:user/owner"

    def aws(*argv):
        if argv[:2] == ("eks", "describe-cluster"):
            return {
                "cluster": {
                    "arn": context,
                    "status": "ACTIVE",
                    "endpoint": "https://example.gr7.us-east-1.eks.amazonaws.com",
                    "certificateAuthority": {
                        "data": base64.b64encode(b"test-public-ca").decode()
                    },
                }
            }
        assert argv[:2] == ("ecr", "describe-images")
        return {
            "imageDetails": [
                {"imageDigest": argv[-1].split("=", 1)[1], "imageTags": [sha]}
            ]
        }

    cloud = SimpleNamespace(
        args=SimpleNamespace(account=account, profile="strawhats"),
        context=context,
        window=(12, datetime.now(timezone.utc) + timedelta(hours=24)),
        aws=aws,
        owner=lambda arn: None,
        stack=lambda: (
            {
                "StackId": f"arn:aws:cloudformation:us-east-1:{account}:stack/strawhats-midterm/id",
                "Parameters": [
                    {"ParameterKey": "OwnerPrincipalArn", "ParameterValue": owner}
                ],
            },
            {"ClusterArn": context},
        ),
    )
    monkeypatch.setattr(owner_release, "LOCAL", tmp_path)
    first = owner_release.prepare_receipt(cloud, images)
    original = Path(first.kubeconfig).read_bytes()
    second = owner_release.prepare_receipt(cloud, images)
    assert first.phase == "prepared" and first.inventory == ()
    assert first.kubeconfig != second.kubeconfig
    assert Path(first.kubeconfig).read_bytes() == original


def test_rehearsal_receipt_rejects_an_image_from_another_source(connection):
    with pytest.raises(ValueError):
        replace(
            connection.receipt,
            source_sha="b" * 40,
            images=replace(connection.receipt.images, source_sha="b" * 40),
        )


def test_delete_rechecks_guard_after_its_uid_read(runtime):
    from kubernetes.client.exceptions import ApiException

    record = runtime.contract.ObjectRecord(
        "v1", "ConfigMap", "strawhats", "config", "captured", "created", "a" * 64
    )
    calls = []

    class Resource:
        deleted = False

        def get(self, **kwargs):
            if self.deleted:
                raise ApiException(404)
            return SimpleNamespace(to_dict=lambda: {"metadata": {"uid": "captured"}})

        def delete(self, **kwargs):
            self.deleted = True
            calls.append(kwargs)

    objects = object.__new__(runtime.install.RuntimeObjects)
    resource = Resource()
    objects._resource = lambda doc: resource

    def guard():
        raise ValueError("Window expired during the UID read")

    objects.before_mutation = guard
    with pytest.raises(ValueError):
        objects.delete(record)
    assert calls == []
