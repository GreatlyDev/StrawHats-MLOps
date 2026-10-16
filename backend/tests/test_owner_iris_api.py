"""Private runtime routes retain local guards and actual deployment provenance."""

import importlib
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from kubernetes import client

from backend.app.iris_cluster import IrisCluster
from backend.app.iris_target import RuntimeTarget
from backend.app.owner_target import OwnerConnection
from backend.app.store import Store
from backend.model_operator.spec import choose_target
from backend.model_operator.workload import render_workload


@pytest.fixture
def owner_api(monkeypatch, tmp_path):
    try:
        routes = importlib.import_module("backend.app.iris_routes")
    except ModuleNotFoundError:
        pytest.fail("Shared private Iris routes are not implemented")
    registry = "123456789012.dkr.ecr.us-east-1.amazonaws.com/strawhats-midterm/"
    target = RuntimeTarget(
        "eks",
        "arn:aws:eks:us-east-1:123456789012:cluster/strawhats-midterm",
        "strawhats",
        "strawhats-monitoring",
        "iris",
        registry + "iris@sha256:" + "a" * 64,
        "IfNotPresent",
        "strawhats-model-operator",
        registry + "operator@sha256:" + "b" * 64,
    )
    h = SimpleNamespace(
        writes=[],
        connects=0,
        available=True,
        guards=0,
        target=target,
        store=Store(tmp_path),
    )
    spec = {
        "mode": "Deploy",
        "image": target.image,
        "imagePullPolicy": "IfNotPresent",
        "replicas": 1,
        "cpuMillicores": 250,
        "memoryMi": 256,
    }
    h.resource = {
        "apiVersion": "mlops.strawhats.dev/v1alpha1",
        "kind": "ModelDeployment",
        "metadata": {
            "name": "iris",
            "namespace": "strawhats",
            "uid": "model-uid",
            "generation": 8,
            "resourceVersion": "10",
        },
        "spec": deepcopy(spec),
        "status": {
            "phase": "Ready",
            "observedGeneration": 8,
            "readyReplicas": 1,
            "image": target.image,
            "lastReadySpec": deepcopy(spec),
        },
    }

    def read(*args, **kwargs):
        assert args[2:5] == ("strawhats", "modeldeployments", "iris")
        return deepcopy(h.resource)

    def patch(*args, body, **kwargs):
        assert args[2:5] == ("strawhats", "modeldeployments", "iris")
        assert body["metadata"] == {"uid": "model-uid", "resourceVersion": "10"}
        h.writes.append(deepcopy(body))
        h.resource["metadata"]["generation"] += 1
        h.resource["spec"].update(body["spec"])
        return deepcopy(h.resource)

    def controller(*args, **kwargs):
        assert args == ("strawhats-model-operator", "strawhats")
        return {
            "metadata": {"generation": 1},
            "status": {
                "observedGeneration": 1,
                "readyReplicas": 1,
                "availableReplicas": 1,
                "updatedReplicas": 1,
            },
        }

    def guard():
        h.guards += 1
        if not h.available:
            raise HTTPException(503, "Release window is unavailable")

    with client.ApiClient() as sdk:
        cluster = IrisCluster(
            sdk,
            SimpleNamespace(
                get_namespaced_custom_object=read, patch_namespaced_custom_object=patch
            ),
            SimpleNamespace(read_namespaced_deployment=controller),
            target=target,
            before_mutation=guard,
        )
        receipt = SimpleNamespace(
            inventory=(
                SimpleNamespace(
                    kind="ModelDeployment",
                    name="iris",
                    namespace="strawhats",
                    uid="model-uid",
                ),
            )
        )

        @contextmanager
        def connect():
            h.connects += 1
            if not h.available:
                raise HTTPException(503, "Release window is unavailable")
            yield OwnerConnection(
                target, cluster, receipt, None, "operate", None, guard
            )

        h.connect = connect

        def service(name, namespace, **kwargs):
            assert (name, namespace) == ("iris", "strawhats")
            return render_workload(h.resource, choose_target(h.resource))[1]

        monkeypatch.setattr(
            client,
            "CoreV1Api",
            lambda sdk: SimpleNamespace(read_namespaced_service=service),
        )

        def prediction(path, method, **kwargs):
            assert (
                method == "POST" and kwargs["path_params"]["namespace"] == "strawhats"
            )
            return {
                "label": "setosa",
                "confidence": 0.98,
                "probabilities": {
                    "setosa": 0.98,
                    "versicolor": 0.01,
                    "virginica": 0.01,
                },
                "latency_ms": 1.5,
            }

        monkeypatch.setattr(sdk, "call_api", prediction)
        h.app = FastAPI()
        h.app.include_router(
            routes.iris_router(
                prefix="/api/eks-iris",
                label="AWS EKS Iris",
                enabled=True,
                connect=connect,
                record_event=h.store.event,
            )
        )
        with TestClient(
            h.app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 5000)
        ) as api:
            h.api = api
            yield h


def test_cloud_action_is_bound_pending_and_records_real_provenance(owner_api):
    h = owner_api
    before = h.store.overview()
    status = h.api.get("/api/eks-iris").json()
    assert status["context"] == h.target.context and status["namespace"] == "strawhats"
    result = h.api.post("/api/eks-iris/scale", json={"replicas": 2})
    assert result.status_code == 202 and result.json()["phase"] == "Pending"
    assert h.guards == 1 and h.writes[0]["spec"]["image"] == h.target.image
    assert h.store.overview() == before
    assert h.target.context in h.store.events()[0]["description"]


def test_prediction_comes_from_bound_service_without_workspace_count(owner_api):
    h = owner_api
    before = h.store.overview()
    response = h.api.post(
        "/api/eks-iris/predict", json={"features": [5.1, 3.5, 1.4, 0.2]}
    )
    assert response.status_code == 200
    assert response.json()["context"] == h.target.context
    assert (
        response.json()["namespace"] == "strawhats"
        and response.json()["generation"] == 8
    )
    assert h.store.overview() == before and h.writes == []


@pytest.mark.parametrize(
    "endpoint,payload",
    [
        ("scale", {"replicas": 0}),
        ("scale", {"replicas": 3}),
        ("deploy", {"replicas": True}),
        ("deploy", {"replicas": 1, "namespace": "foreign"}),
        ("rollback", {"image": "foreign"}),
        ("predict", {"features": [True, 3.5, 1.4, 0.2]}),
        ("predict", {"features": [31, 3.5, 1.4, 0.2]}),
    ],
)
def test_invalid_input_never_opens_a_connection(owner_api, endpoint, payload):
    result = owner_api.api.post("/api/eks-iris/" + endpoint, json=payload)
    assert (
        result.status_code == 422 and owner_api.connects == 0 and owner_api.writes == []
    )


@pytest.mark.parametrize(
    "fault", ["caller", "host", "origin", "content_type", "nonfinite"]
)
def test_private_route_guards_precede_connection(owner_api, fault):
    h = owner_api
    with TestClient(
        h.app,
        base_url="http://evil.test" if fault == "host" else "http://127.0.0.1:8000",
        client=("203.0.113.1" if fault == "caller" else "127.0.0.1", 5000),
    ) as api:
        headers = {"Origin": "https://foreign.test"} if fault == "origin" else {}
        if fault == "content_type":
            result = api.post(
                "/api/eks-iris/scale",
                content='{"replicas":2}',
                headers={"Content-Type": "text/plain"},
            )
        elif fault == "nonfinite":
            result = api.post(
                "/api/eks-iris/predict",
                content='{"features":[NaN,3.5,1.4,0.2]}',
                headers={"Content-Type": "application/json"},
            )
        else:
            result = api.post(
                "/api/eks-iris/scale", json={"replicas": 2}, headers=headers
            )
    assert result.status_code in (403, 415, 422) and h.connects == 0 and h.writes == []


def test_unavailable_cloud_never_returns_local_success(owner_api):
    owner_api.available = False
    result = owner_api.api.post("/api/eks-iris/deploy", json={"replicas": 1})
    assert result.status_code == 503 and owner_api.writes == []


def test_main_descriptor_and_test_mode_never_use_real_aws(
    owner_api, monkeypatch, tmp_path
):
    from backend.app import main

    monkeypatch.setattr(main, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("OWNER_CLUSTER_TARGET", "eks")
    monkeypatch.setenv("LOCAL_OPERATOR_ENABLED", "0")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setattr(
        main, "owner_connection", lambda *a, **k: owner_api.connect(), raising=False
    )
    with TestClient(
        main.create_app(tmp_path / "normal"),
        base_url="http://127.0.0.1:8000",
        client=("127.0.0.1", 5000),
    ) as api:
        descriptor = api.get("/api/owner-target")
        assert (
            descriptor.status_code == 200
            and descriptor.json()["api_prefix"] == "/eks-iris"
        )
        assert (
            "context" not in descriptor.json() and "connected" not in descriptor.json()
        )
        local = api.get("/api/local-iris").json()
        assert local["enabled"] is False and local["context"] == "docker-desktop"
        assert api.get("/api/eks-iris").json()["context"] == owner_api.target.context
    calls = owner_api.connects
    with TestClient(main.create_app(tmp_path / "test", test_mode=True)) as api:
        assert api.get("/api/owner-target").json()["kind"] == "disabled"
        assert api.post("/api/eks-iris/deploy", json={"replicas": 1}).status_code == 503
    assert owner_api.connects == calls
