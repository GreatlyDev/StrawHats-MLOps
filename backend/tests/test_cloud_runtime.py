"""Rendered runtime and UID-bound ownership behavior without a real cluster."""

import importlib
import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import yaml

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

        def delete(self, record, *, before_delete=lambda: None):
            before_delete()
            doc = record.document()
            current = self.data.get(key(doc))
            if current and current["metadata"]["uid"] != record.uid:
                raise RuntimeError("UID changed")
            self.deleted.append(record)
            self.data.pop(key(doc), None)

        def available_capacity(self):
            return self.free

        def instances(self, crd):
            return [d for d in self.data.values() if d["kind"] == "ModelDeployment"]

        def children(self, namespace):
            return [
                d
                for d in self.data.values()
                if d["metadata"].get("namespace") == namespace
            ] + [
                d
                for d in self.foreign_children
                if d["metadata"].get("namespace") == namespace
            ]

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


@pytest.mark.parametrize("kind", ["eks", "rehearsal"])
def test_grafana_datasource_rebinds_url_and_preserves_other_settings(
    runtime, rehearsal, kind
):
    target = rehearsal
    if kind == "eks":
        registry = "123456789012.dkr.ecr.us-east-1.amazonaws.com/strawhats-midterm/"
        target = replace(
            rehearsal,
            kind="eks",
            context="arn:aws:eks:us-east-1:123456789012:cluster/strawhats-midterm",
            image=registry + "iris@sha256:" + "a" * 64,
            operator_image=registry + "operator@sha256:" + "b" * 64,
            image_pull_policy="IfNotPresent",
        )
    source = runtime.manifests.BASE / "monitoring/configs/datasources.yaml"
    original = source.read_bytes()
    expected = yaml.safe_load(original)
    expected["datasources"][0]["url"] = (
        "http://prometheus.strawhats-monitoring.svc:9090"
    )

    bundle = runtime.manifests.render_runtime(target)
    config = next(
        doc
        for doc in bundle.documents
        if doc["kind"] == "ConfigMap"
        and doc["metadata"]["name"] == "grafana-datasource"
    )

    assert config["metadata"]["namespace"] == "strawhats-monitoring"
    assert yaml.safe_load(config["data"]["datasources.yaml"]) == expected
    assert source.read_bytes() == original


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


@pytest.fixture
def discovery_objects(runtime, monkeypatch, tmp_path):
    """Use the real persistent SDK discovery cache with an offline API boundary."""
    from kubernetes import client, dynamic
    from kubernetes.client.exceptions import ApiException

    group = "mlops.strawhats.dev"
    crd_name = "modeldeployments." + group
    crd_path = "/apis/apiextensions.k8s.io/v1/customresourcedefinitions/" + crd_name
    server = SimpleNamespace(
        crd_present=True,
        deleted=[],
        children=[],
        list_error=None,
        list_errors=[],
        list_calls=0,
    )
    crd = {
        "apiVersion": "apiextensions.k8s.io/v1",
        "kind": "CustomResourceDefinition",
        "metadata": {"name": crd_name, "uid": "captured"},
    }

    def description(name, kind, namespaced):
        return {
            "name": name,
            "kind": kind,
            "namespaced": namespaced,
            "verbs": ["get", "list", "delete"],
        }

    def call_api(path, method, *args, **kwargs):
        if path == "/version":
            value = {"gitVersion": "v1.35.0"}
        elif path == "/apis":
            groups = [("apiextensions.k8s.io", "v1")]
            if server.crd_present:
                groups.append((group, "v1alpha1"))
            value = {
                "kind": "APIGroupList",
                "apiVersion": "v1",
                "groups": [
                    {
                        "name": name,
                        "versions": [{"groupVersion": f"{name}/{v}", "version": v}],
                        "preferredVersion": {
                            "groupVersion": f"{name}/{v}",
                            "version": v,
                        },
                    }
                    for name, v in groups
                ],
            }
        elif path == "/api/v1":
            value = {
                "kind": "APIResourceList",
                "apiVersion": "v1",
                "groupVersion": "v1",
                "resources": [description("configmaps", "ConfigMap", True)],
            }
        elif path == "/apis/apiextensions.k8s.io/v1":
            value = {
                "kind": "APIResourceList",
                "apiVersion": "v1",
                "groupVersion": "apiextensions.k8s.io/v1",
                "resources": [
                    description(
                        "customresourcedefinitions", "CustomResourceDefinition", False
                    )
                ],
            }
        elif path == crd_path:
            if not server.crd_present:
                raise ApiException(404)
            if method == "DELETE":
                assert kwargs["body"]["preconditions"] == {"uid": "captured"}
                server.deleted.append("captured")
                server.crd_present = False
                value = {"kind": "Status", "status": "Success"}
            else:
                assert method == "GET"
                value = crd
        elif path.startswith(f"/apis/{group}/v1alpha1"):
            if not server.crd_present:
                raise ApiException(404)
            value = (
                {
                    "kind": "APIResourceList",
                    "apiVersion": "v1",
                    "groupVersion": f"{group}/v1alpha1",
                    "resources": [
                        description("modeldeployments", "ModelDeployment", True)
                    ],
                }
                if path == f"/apis/{group}/v1alpha1"
                else {
                    "kind": "ModelDeploymentList",
                    "apiVersion": f"{group}/v1alpha1",
                    "items": [],
                }
            )
        elif path == "/api/v1/namespaces/strawhats/configmaps":
            server.list_calls += 1
            if server.list_errors:
                status, headers = server.list_errors.pop(0)
                error = ApiException(status)
                error.headers = headers
                raise error
            if server.list_error:
                raise ApiException(server.list_error)
            value = {
                "kind": "ConfigMapList",
                "apiVersion": "v1",
                "items": server.children,
            }
        else:
            pytest.fail(f"Unexpected offline Kubernetes request: {method} {path}")
        return SimpleNamespace(data=json.dumps(value).encode())

    sdk = client.ApiClient(client.Configuration(host="https://cleanup.invalid"))
    monkeypatch.setattr(sdk, "call_api", call_api)
    dynamic_client = dynamic.DynamicClient
    monkeypatch.setattr(
        runtime.install.dynamic,
        "DynamicClient",
        lambda sdk: dynamic_client(sdk, cache_file=str(tmp_path / "discovery.json")),
    )

    def make():
        return runtime.install.RuntimeObjects(sdk)

    objects = make()
    # Populate the SDK's persisted cache while the custom API still exists.
    assert any(
        resource.kind == "ModelDeployment"
        for resource in objects.dynamic.resources.search(namespaced=True)
    )
    return SimpleNamespace(server=server, crd=crd, objects=objects, make=make)


@pytest.mark.parametrize("already_absent", [False, True])
def test_crd_cleanup_refreshes_persisted_discovery(
    runtime, discovery_objects, already_absent
):
    api = discovery_objects
    api.server.crd_present = not already_absent
    record = runtime.contract.ObjectRecord(
        "apiextensions.k8s.io/v1",
        "CustomResourceDefinition",
        "",
        "modeldeployments.mlops.strawhats.dev",
        "captured",
        "created",
        "a" * 64,
    )

    api.objects.delete(record)

    # A new client must not inherit an API that this cleanup confirmed removed.
    assert not any(
        resource.kind == "ModelDeployment"
        for resource in api.make().dynamic.resources.search(namespaced=True)
    )
    assert api.server.deleted == ([] if already_absent else ["captured"])


def test_namespace_children_refreshes_persisted_discovery(discovery_objects):
    api = discovery_objects
    api.server.crd_present = False
    api.server.children = [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"namespace": "strawhats", "name": "foreign", "uid": "foreign"},
        }
    ]

    assert api.make().children("strawhats") == api.server.children


@pytest.mark.parametrize("status", [403, 404, 429])
def test_namespace_children_propagates_current_api_errors(discovery_objects, status):
    from kubernetes.client.exceptions import ApiException

    api = discovery_objects
    api.server.crd_present = False
    api.server.list_error = status

    with pytest.raises(ApiException) as result:
        api.make().children("strawhats")
    assert result.value.status == status


@pytest.mark.parametrize("header", ["Retry-After", "retry-after", "rEtRy-AfTeR"])
@pytest.mark.parametrize("failures,delay", [(1, "1"), (2, "5")])
def test_namespace_children_retries_short_throttling_without_losing_foreign_child(
    runtime, discovery_objects, monkeypatch, header, failures, delay
):
    api = discovery_objects
    api.server.crd_present = False
    foreign = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"namespace": "strawhats", "name": "foreign", "uid": "foreign"},
    }
    api.server.children = [foreign]
    api.server.list_errors = [(429, {header: delay}) for _ in range(failures)]
    waits = []
    monkeypatch.setattr(runtime.install.time, "sleep", waits.append)

    assert api.make().children("strawhats") == [foreign]
    assert api.server.list_calls == failures + 1
    assert waits == [int(delay)] * failures


def test_namespace_children_stops_after_three_throttled_attempts(
    runtime, discovery_objects, monkeypatch
):
    from kubernetes.client.exceptions import ApiException

    api = discovery_objects
    api.server.crd_present = False
    api.server.list_errors = [(429, {"Retry-After": "1"}) for _ in range(4)]
    waits = []
    monkeypatch.setattr(runtime.install.time, "sleep", waits.append)

    with pytest.raises(ApiException) as result:
        api.make().children("strawhats")
    assert result.value.status == 429
    assert api.server.list_calls == 3
    assert waits == [1, 1]


@pytest.mark.parametrize(
    "status,headers",
    [
        (403, {"Retry-After": "1"}),
        (404, {"Retry-After": "1"}),
        (429, None),
        (429, {}),
        *[
            (429, {"Retry-After": value})
            for value in (
                "0",
                "6",
                "1.0",
                "-1",
                "abc",
                "1 ",
                "Sat, 17 Oct 2026 20:00:00 GMT",
                None,
                1,
            )
        ],
    ],
)
def test_namespace_children_never_retries_other_or_invalid_throttling(
    runtime, discovery_objects, monkeypatch, status, headers
):
    from kubernetes.client.exceptions import ApiException

    api = discovery_objects
    api.server.crd_present = False
    api.server.list_errors = [(status, headers)]
    waits = []
    monkeypatch.setattr(runtime.install.time, "sleep", waits.append)

    with pytest.raises(ApiException) as result:
        api.make().children("strawhats")
    assert result.value.status == status
    assert api.server.list_calls == 1
    assert waits == []


@pytest.mark.parametrize("late", [False, True])
def test_created_crd_cleanup_preserves_foreign_instances_cluster_wide(
    runtime, connection, tmp_path, late
):
    runtime.install.install_runtime(
        connection,
        runtime.manifests.render_runtime(connection.target),
        evidence_dir=tmp_path,
    )
    foreign = {
        "apiVersion": "mlops.strawhats.dev/v1alpha1",
        "kind": "ModelDeployment",
        "metadata": {"namespace": "other-team", "name": "model", "uid": "foreign"},
    }
    if not late:
        connection.kube.data[key(foreign)] = foreign
    else:
        delete = connection.kube.delete

        def raced(record, **kwargs):
            if record.kind == "CustomResourceDefinition":
                connection.kube.data[key(foreign)] = foreign
            delete(record, **kwargs)

        connection.kube.delete = raced
    connection.purpose = "cleanup"
    with pytest.raises(RuntimeError, match="foreign custom resource"):
        runtime.install.remove_runtime(connection, connection.receipt)
    assert connection.kube.get(foreign) == foreign
    assert not any(
        r.kind == "CustomResourceDefinition" for r in connection.kube.deleted
    )
    if not late:
        assert not connection.kube.deleted


@pytest.mark.parametrize("kind", ["Deployment", "CustomResourceDefinition"])
def test_resume_rejects_live_configuration_drift_under_same_uid(
    runtime, connection, tmp_path, kind
):
    bundle = runtime.manifests.render_runtime(connection.target)
    runtime.install.install_runtime(connection, bundle, evidence_dir=tmp_path)
    doc = next(d for d in connection.kube.data.values() if d["kind"] == kind)
    if kind == "Deployment":
        doc["spec"]["template"]["spec"]["containers"][0]["image"] = "foreign:latest"
    else:
        doc["spec"]["versions"][0]["served"] = False
    count = len(connection.kube.writes)
    with pytest.raises(RuntimeError, match="configuration drift"):
        runtime.install.install_runtime(connection, bundle, evidence_dir=tmp_path)
    assert len(connection.kube.writes) == count


@pytest.mark.parametrize("bounded", [True, False])
def test_resume_preserves_only_bounded_release_model_lifecycle(
    runtime, connection, tmp_path, bounded
):
    bundle = runtime.manifests.render_runtime(connection.target)
    runtime.install.install_runtime(connection, bundle, evidence_dir=tmp_path)
    model = next(
        d for d in connection.kube.data.values() if d["kind"] == "ModelDeployment"
    )
    model["spec"].update(
        replicas=2, cpuMillicores=500 if bounded else 501, memoryMi=512, mode="Rollback"
    )
    model["status"] = {"lastReadySpec": deepcopy(model["spec"])}
    before = deepcopy(model)
    if bounded:
        runtime.install.install_runtime(connection, bundle, evidence_dir=tmp_path)
        assert model == before
    else:
        with pytest.raises(RuntimeError, match="configuration drift"):
            runtime.install.install_runtime(connection, bundle, evidence_dir=tmp_path)


def test_borrowed_crd_keeps_original_labels_and_rejects_later_schema_drift(
    runtime, connection, tmp_path
):
    import yaml

    crd = yaml.safe_load((runtime.manifests.BASE / "operator/crd.yaml").read_text())
    crd["metadata"]["uid"] = "borrowed-original"
    connection.kube.data[key(crd)] = deepcopy(crd)
    bundle = runtime.manifests.render_runtime(connection.target)
    runtime.install.install_runtime(connection, bundle, evidence_dir=tmp_path)
    assert connection.kube.get(crd) == crd
    assert (
        next(
            r
            for r in connection.receipt.inventory
            if r.kind == "CustomResourceDefinition"
        ).ownership
        == "borrowed"
    )
    runtime.install.install_runtime(connection, bundle, evidence_dir=tmp_path)
    connection.kube.data[key(crd)]["spec"]["versions"][0]["served"] = False
    with pytest.raises(RuntimeError, match="configuration drift"):
        runtime.install.install_runtime(connection, bundle, evidence_dir=tmp_path)
