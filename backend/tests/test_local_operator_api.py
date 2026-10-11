"""The dashboard must never imply a disabled local controller is available."""

import os
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

from backend.app.main import create_app


def local_client(tmp_path, **kwargs):
    return TestClient(
        create_app(data_dir=tmp_path, test_mode=True, **kwargs),
        base_url="http://127.0.0.1:8000",
        client=("127.0.0.1", 50000),
    )


def test_local_status_is_disabled_without_explicit_enablement(tmp_path):
    with local_client(tmp_path) as api:
        response = api.get("/api/local-iris")
        assert response.status_code == 200
        assert response.json()["enabled"] is False
        assert response.json()["connected"] is False
        assert response.json()["phase"] == "Disabled"
        assert response.json()["rollback_available"] is False


@pytest.mark.parametrize("action", ["deploy", "scale", "rollback"])
def test_disabled_actions_do_not_change_the_workspace(tmp_path, action):
    with local_client(tmp_path) as api:
        before = api.get("/api/overview").json()
        response = api.post(
            f"/api/local-iris/{action}",
            json={} if action == "rollback" else {"replicas": 1},
        )
        assert response.status_code == 503
        assert api.get("/api/overview").json() == before
        assert api.get("/api/events").json() == []


class LocalCluster:
    """External Kubernetes boundary with versioned writes and realistic status."""

    def __init__(self):
        self.resource = {
            "apiVersion": "mlops.strawhats.dev/v1alpha1",
            "kind": "ModelDeployment",
            "metadata": {
                "name": "iris",
                "namespace": "strawhats-operator-local",
                "uid": "iris-uid",
                "generation": 7,
                "resourceVersion": "40",
            },
            "spec": {
                "mode": "Deploy",
                "image": "strawhats-iris:milestone-one",
                "imagePullPolicy": "Never",
                "replicas": 1,
                "cpuMillicores": 250,
                "memoryMi": 256,
            },
            "status": {
                "observedGeneration": 7,
                "phase": "Ready",
                "image": "strawhats-iris:milestone-one",
                "readyReplicas": 1,
                "replicas": 1,
                "lastReadySpec": {
                    "image": "strawhats-iris:milestone-one",
                    "imagePullPolicy": "Never",
                    "replicas": 1,
                    "cpuMillicores": 250,
                    "memoryMi": 256,
                },
                "conditions": [
                    {"type": "Ready", "status": "True", "message": "Verified"}
                ],
            },
        }
        self.controller_ready = True
        self.writes = []
        self.error = None
        self.conflict = False
        self.loaded_contexts = []
        configuration = client.Configuration()
        configuration.host = "https://127.0.0.1:6443"
        self.sdk = client.ApiClient(configuration)

    def read_namespaced_deployment(self, name, namespace, **kwargs):
        assert (name, namespace) == (
            "strawhats-model-operator",
            "strawhats-operator-local",
        )
        if self.error:
            raise self.error
        return {
            "metadata": {"generation": 1},
            "status": {
                "observedGeneration": 1,
                "readyReplicas": int(self.controller_ready),
                "updatedReplicas": int(self.controller_ready),
                "availableReplicas": int(self.controller_ready),
            },
        }

    def get_namespaced_custom_object(
        self, group, version, namespace, plural, name, **kwargs
    ):
        assert (group, version, namespace, plural, name) == (
            "mlops.strawhats.dev",
            "v1alpha1",
            "strawhats-operator-local",
            "modeldeployments",
            "iris",
        )
        if self.resource is None:
            raise ApiException(status=404)
        return deepcopy(self.resource)

    def create_namespaced_custom_object(
        self, group, version, namespace, plural, body, **kwargs
    ):
        assert (group, version, namespace, plural) == (
            "mlops.strawhats.dev",
            "v1alpha1",
            "strawhats-operator-local",
            "modeldeployments",
        )
        if self.conflict or self.resource is not None:
            raise ApiException(status=409, reason="credential=do-not-expose")
        self.writes.append(deepcopy(body))
        self.resource = deepcopy(body)
        self.resource["metadata"].update(
            uid="new-uid", generation=1, resourceVersion="1"
        )
        return deepcopy(self.resource)

    def patch_namespaced_custom_object(
        self, group, version, namespace, plural, name, body, **kwargs
    ):
        assert (group, version, namespace, plural, name) == (
            "mlops.strawhats.dev",
            "v1alpha1",
            "strawhats-operator-local",
            "modeldeployments",
            "iris",
        )
        if self.conflict:
            raise ApiException(status=409, reason="credential=do-not-expose")
        assert (
            body["metadata"]["resourceVersion"]
            == self.resource["metadata"]["resourceVersion"]
        )
        assert set(body) == {"metadata", "spec"}  # The UI must never write status.
        self.writes.append(deepcopy(body))
        self.resource["spec"].update(body["spec"])
        self.resource["metadata"]["generation"] += 1
        self.resource["metadata"]["resourceVersion"] = "41"
        return deepcopy(self.resource)


@pytest.fixture
def enabled_api(tmp_path, monkeypatch):
    from backend.app import main

    cluster = LocalCluster()
    monkeypatch.setenv("LOCAL_OPERATOR_ENABLED", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setattr(main, "load_dotenv", lambda *args, **kwargs: None)

    def connect(*args, **kwargs):
        cluster.loaded_contexts.append(kwargs.get("context"))
        return cluster.sdk

    monkeypatch.setattr(config, "new_client_from_config", connect)
    monkeypatch.setattr(client, "CustomObjectsApi", lambda sdk: cluster)
    monkeypatch.setattr(client, "AppsV1Api", lambda sdk: cluster)
    with TestClient(
        create_app(data_dir=tmp_path),
        base_url="http://127.0.0.1:8000",
        client=("127.0.0.1", 50000),
    ) as api:
        yield api, cluster
    cluster.sdk.close()


def test_observed_status_reads_only_the_explicit_local_context(enabled_api):
    api, cluster = enabled_api
    response = api.get("/api/local-iris")
    assert response.status_code == 200
    result = response.json()
    assert result["enabled"] and result["connected"] and result["operator_ready"]
    assert result["phase"] == "Ready"
    assert result["desired_replicas"] == result["ready_replicas"] == 1
    assert result["generation"] == result["observed_generation"] == 7
    assert result["rollback_available"] is True
    assert cluster.loaded_contexts == ["docker-desktop"]
    assert cluster.writes == []


def test_absent_iris_is_not_reported_as_a_ready_deployment(enabled_api):
    api, cluster = enabled_api
    cluster.resource = None
    response = api.get("/api/local-iris")
    assert response.status_code == 200
    assert response.json()["phase"] == "NotDeployed"
    assert response.json()["generation"] is None
    assert response.json()["rollback_available"] is False


def test_unobserved_generation_is_pending_even_with_old_ready_status(enabled_api):
    api, cluster = enabled_api
    cluster.resource["metadata"]["generation"] = 8
    result = api.get("/api/local-iris").json()
    assert result["phase"] == "Pending"
    assert result["generation"] == 8
    assert result["observed_generation"] == 7


def test_deploy_creates_only_the_fixed_cr_and_reports_acceptance(enabled_api):
    api, cluster = enabled_api
    cluster.resource = None
    response = api.post("/api/local-iris/deploy", json={"replicas": 1})
    assert response.status_code == 202
    assert response.json()["phase"] == "Pending"
    assert response.json()["ready_replicas"] == 0
    assert cluster.writes == [
        {
            "apiVersion": "mlops.strawhats.dev/v1alpha1",
            "kind": "ModelDeployment",
            "metadata": {"name": "iris", "namespace": "strawhats-operator-local"},
            "spec": {
                "mode": "Deploy",
                "image": "strawhats-iris:milestone-one",
                "imagePullPolicy": "Never",
                "replicas": 1,
                "cpuMillicores": 250,
                "memoryMi": 256,
            },
        }
    ]
    assert "requested" in api.get("/api/events").json()[0]["title"].lower()


def test_scaling_preserves_the_verified_target_after_rollback(enabled_api):
    api, cluster = enabled_api
    cluster.resource["spec"].update(mode="Rollback", image="strawhats-iris:missing")
    cluster.resource["status"]["phase"] = "RolledBack"
    response = api.post("/api/local-iris/scale", json={"replicas": 2})
    assert response.status_code == 202
    assert response.json()["phase"] == "Pending"
    assert response.json()["desired_replicas"] == 2
    assert cluster.resource["spec"] == {
        "mode": "Deploy",
        "image": "strawhats-iris:milestone-one",
        "imagePullPolicy": "Never",
        "replicas": 2,
        "cpuMillicores": 250,
        "memoryMi": 256,
    }
    assert cluster.writes[0]["metadata"] == {"resourceVersion": "40"}


def test_rollback_changes_mode_without_overwriting_snapshot_or_desired_image(
    enabled_api,
):
    api, cluster = enabled_api
    cluster.resource["spec"]["image"] = "strawhats-iris:missing"
    cluster.resource["status"]["phase"] = "Failed"
    snapshot = deepcopy(cluster.resource["status"]["lastReadySpec"])
    response = api.post("/api/local-iris/rollback", json={})
    assert response.status_code == 202
    assert response.json()["phase"] == "Pending"
    assert cluster.resource["spec"]["mode"] == "Rollback"
    assert cluster.resource["spec"]["image"] == "strawhats-iris:missing"
    assert cluster.resource["status"]["lastReadySpec"] == snapshot
    assert cluster.writes[0]["spec"] == {"mode": "Rollback"}


@pytest.mark.parametrize("replicas", [0, 3, True, 1.5, "2"])
def test_invalid_replica_changes_never_contact_kubernetes(enabled_api, replicas):
    api, cluster = enabled_api
    assert (
        api.post("/api/local-iris/scale", json={"replicas": replicas}).status_code
        == 422
    )
    assert cluster.loaded_contexts == []
    assert cluster.writes == []


@pytest.mark.parametrize(
    "extra", [{"namespace": "default"}, {"image": "other:latest"}, {"name": "other"}]
)
def test_actions_cannot_change_the_fixed_target(enabled_api, extra):
    api, cluster = enabled_api
    response = api.post("/api/local-iris/deploy", json={"replicas": 1, **extra})
    assert response.status_code == 422
    assert cluster.loaded_contexts == []
    assert cluster.writes == []


@pytest.mark.parametrize("phase", ["Pending", "Failed", "Conflict"])
def test_scale_requires_a_verified_ready_revision(enabled_api, phase):
    api, cluster = enabled_api
    cluster.resource["status"]["phase"] = phase
    assert api.post("/api/local-iris/scale", json={"replicas": 2}).status_code == 409
    assert cluster.writes == []


def test_missing_rollback_snapshot_is_a_conflict_without_writes(enabled_api):
    api, cluster = enabled_api
    del cluster.resource["status"]["lastReadySpec"]
    assert api.post("/api/local-iris/rollback", json={}).status_code == 409
    assert cluster.writes == []


@pytest.mark.parametrize("action", ["deploy", "scale", "rollback"])
def test_version_conflicts_do_not_record_success_events(enabled_api, action):
    api, cluster = enabled_api
    if action == "deploy":
        cluster.resource = None
    cluster.conflict = True
    response = api.post(
        f"/api/local-iris/{action}",
        json={} if action == "rollback" else {"replicas": 2},
    )
    assert response.status_code == 409
    assert "do-not-expose" not in response.text
    assert cluster.writes == []
    assert api.get("/api/events").json() == []


def test_offline_controller_disables_actions_and_does_not_show_old_ready_phase(
    enabled_api,
):
    api, cluster = enabled_api
    cluster.controller_ready = False
    result = api.get("/api/local-iris").json()
    assert result["phase"] == "Unavailable"
    assert result["operator_ready"] is False
    assert api.post("/api/local-iris/deploy", json={"replicas": 1}).status_code == 503
    assert cluster.writes == []


@pytest.mark.parametrize("host", ["https://example.com:6443", "http://127.0.0.1:6443"])
def test_remote_or_insecure_kubernetes_endpoint_is_refused(enabled_api, host):
    api, cluster = enabled_api
    cluster.sdk.configuration.host = host
    response = api.post("/api/local-iris/deploy", json={"replicas": 1})
    assert response.status_code == 503
    assert cluster.writes == []


@pytest.mark.parametrize("code,expected", [(403, 403), (500, 503)])
def test_cluster_errors_are_actionable_without_exposing_details(
    enabled_api, code, expected
):
    api, cluster = enabled_api
    cluster.error = ApiException(status=code, reason="credential=do-not-expose")
    response = api.get("/api/local-iris")
    assert response.status_code == expected
    assert "do-not-expose" not in response.text
    assert cluster.writes == []


def test_foreign_browser_origin_cannot_use_local_cluster_credentials(enabled_api):
    api, cluster = enabled_api
    response = api.post(
        "/api/local-iris/deploy",
        json={"replicas": 1},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403
    assert cluster.loaded_contexts == []


def test_nonlocal_api_host_cannot_use_local_cluster_credentials(enabled_api):
    api, cluster = enabled_api
    response = api.post(
        "http://evil.example/api/local-iris/deploy", json={"replicas": 1}
    )
    assert response.status_code == 403
    assert cluster.loaded_contexts == []


def test_remote_client_cannot_use_the_local_api_even_with_a_loopback_host(enabled_api):
    api, cluster = enabled_api
    with TestClient(
        api.app, base_url="http://127.0.0.1:8000", client=("192.0.2.10", 50000)
    ) as remote:
        response = remote.post("/api/local-iris/deploy", json={"replicas": 1})
        assert response.status_code == 403
    assert cluster.loaded_contexts == []


def test_simple_browser_post_is_refused_before_loading_credentials(enabled_api):
    api, cluster = enabled_api
    response = api.post(
        "/api/local-iris/rollback", content="{}", headers={"Content-Type": "text/plain"}
    )
    assert response.status_code == 415
    assert cluster.loaded_contexts == []


def test_repeated_deploy_preserves_a_current_verified_generation(enabled_api):
    api, cluster = enabled_api
    response = api.post("/api/local-iris/deploy", json={"replicas": 1})
    assert response.status_code == 202
    assert response.json()["phase"] == "Ready"
    assert response.json()["generation"] == 7
    assert cluster.writes == []


def test_local_transport_connects_directly_despite_inherited_proxies(
    monkeypatch, tmp_path
):
    from kubernetes.config import kube_config
    from urllib3 import ProxyManager

    from backend.app.local_cluster import connect_local_cluster

    for variable in ("HTTP_PROXY", "HTTPS_PROXY"):
        monkeypatch.setenv(variable, "http://proxy.example:8080")
    monkeypatch.setattr(
        kube_config, "KUBE_CONFIG_DEFAULT_LOCATION", str(tmp_path / "absent-kubeconfig")
    )
    loaded_contexts = []

    def load(*args, **kwargs):
        assert kwargs["context"] == "docker-desktop"
        assert kwargs["persist_config"] is False
        loaded_contexts.append(kwargs["context"])
        kwargs["client_configuration"].host = "https://127.0.0.1:6443"

    monkeypatch.setattr(kube_config, "load_kube_config", load)
    with connect_local_cluster() as cluster:
        assert cluster.sdk.configuration.proxy is None
        assert not isinstance(cluster.sdk.rest_client.pool_manager, ProxyManager)
    assert loaded_contexts == ["docker-desktop"]
    assert os.environ["HTTPS_PROXY"] == "http://proxy.example:8080"


def test_unexpected_kubernetes_proxy_is_refused_before_requests(enabled_api):
    api, cluster = enabled_api
    cluster.sdk.configuration.proxy = "http://proxy.example:8080"
    response = api.post("/api/local-iris/deploy", json={"replicas": 1})
    assert response.status_code == 503
    assert cluster.writes == []
