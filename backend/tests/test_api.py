from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from backend.app.main import create_app


@pytest.fixture
def client(tmp_path: Path):
    with TestClient(create_app(data_dir=tmp_path, test_mode=True)) as api:
        yield api


def train(client):
    response = client.post("/api/models/example")
    assert response.status_code == 201
    return response.json()


def test_example_uses_held_out_evaluation_and_persists(tmp_path):
    with TestClient(create_app(data_dir=tmp_path, test_mode=True)) as api:
        model = train(api)
        assert model["metrics"]["train_samples"] == 120
        assert model["metrics"]["test_samples"] == 30
        assert 0.8 <= model["metrics"]["accuracy"] <= 1.0
        assert len(model["feature_names"]) == 4
    with TestClient(create_app(data_dir=tmp_path, test_mode=True)) as reopened:
        models = reopened.get("/api/models").json()
        assert models[0]["id"] == model["id"]


def test_prediction_classifies_real_input_and_counts_measurement(client):
    model = train(client)
    response = client.post(
        f"/api/models/{model['id']}/predict", json={"features": [5.1, 3.5, 1.4, 0.2]}
    )
    assert response.status_code == 200
    result = response.json()
    assert result["label"] == "setosa"
    assert 0 <= result["confidence"] <= 1
    assert abs(sum(result["probabilities"].values()) - 1) < 1e-6
    assert result["latency_ms"] > 0
    overview = client.get("/api/overview").json()
    assert overview["predictions"] == 1
    assert len(overview["recent_inferences"]) == 1
    assert overview["median_latency_ms"] > 0


@pytest.mark.parametrize(
    "features", [[1, 2], [0, 3.5, 1.4, 0.2], ["bad", 3.5, 1.4, 0.2]]
)
def test_prediction_rejects_invalid_features(client, features):
    train(client)
    response = client.post(
        "/api/models/iris-classifier/predict", json={"features": features}
    )
    assert response.status_code == 422
    assert client.get("/api/overview").json()["predictions"] == 0


def test_prediction_rejects_nonexistent_model(client):
    assert (
        client.post(
            "/api/models/missing/predict", json={"features": [5.1, 3.5, 1.4, 0.2]}
        ).status_code
        == 404
    )


def test_duplicate_example_does_not_overwrite_model(client):
    train(client)
    assert client.post("/api/models/example").status_code == 409
    assert len(client.get("/api/models").json()) == 1


def test_container_registration_is_preserved_and_has_no_local_predictor(client):
    response = client.post(
        "/api/models",
        json={
            "name": "Text classifier",
            "version": "2.0.0",
            "framework": "PyTorch",
            "description": "Containerized classifier",
            "image": "ghcr.io/team/model:v2",
        },
    )
    assert response.status_code == 201
    model = response.json()
    assert model["image"] == "ghcr.io/team/model:v2"
    assert model["kind"] == "container"
    assert (
        client.post(
            f"/api/models/{model['id']}/predict", json={"features": [1, 2, 3, 4]}
        ).status_code
        == 422
    )


def test_preview_yaml_matches_user_configuration(client):
    model = train(client)
    response = client.post(
        "/api/deployment-plans",
        json={
            "model_id": model["id"],
            "name": "iris-service",
            "namespace": "strawhats",
            "image": "ghcr.io/team/iris:v1",
            "replicas": 2,
            "cpu_millicores": 250,
            "memory_mebibytes": 256,
        },
    )
    assert response.status_code == 201
    plan = response.json()
    resources = list(yaml.safe_load_all(plan["manifest"]))
    deployment = next(item for item in resources if item["kind"] == "Deployment")
    assert deployment["metadata"]["namespace"] == "strawhats"
    assert deployment["spec"]["replicas"] == 2
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "ghcr.io/team/iris:v1"
    assert container["resources"]["requests"] == {"cpu": "250m", "memory": "256Mi"}
    assert plan["status"] == "preview"
    assert client.get("/api/deployment-plans").json()[0]["id"] == plan["id"]


def test_preview_cannot_reference_missing_model(client):
    response = client.post(
        "/api/deployment-plans",
        json={
            "model_id": "missing",
            "name": "iris-service",
            "image": "ghcr.io/team/iris:v1",
        },
    )
    assert response.status_code == 404


def test_preview_rejects_invalid_dns_name(client):
    model = train(client)
    response = client.post(
        "/api/deployment-plans",
        json={
            "model_id": model["id"],
            "name": "Bad Name",
            "image": "ghcr.io/team/iris:v1",
        },
    )
    assert response.status_code == 422


def test_overview_starts_empty_without_fabricated_metrics(client):
    assert client.get("/api/overview").json() == {
        "models": 0,
        "deployment_plans": 0,
        "predictions": 0,
        "median_latency_ms": None,
        "recent_inferences": [],
    }


def test_disabled_assistant_returns_actionable_error(client):
    response = client.post(
        "/api/assistant",
        json={"messages": [{"role": "user", "content": "Deploy my model"}]},
    )
    assert response.status_code == 503
    assert "configured" in response.json()["detail"].lower()


def test_metrics_report_observed_predictions(client):
    train(client)
    client.post(
        "/api/models/iris-classifier/predict", json={"features": [5.1, 3.5, 1.4, 0.2]}
    )
    response = client.get("/metrics")
    assert response.status_code == 200
    assert (
        'strawhats_predictions_total{model_id="iris-classifier"} 1.0' in response.text
    )


def test_container_cannot_claim_the_builtin_example_identity(tmp_path):
    client = TestClient(create_app(data_dir=tmp_path, test_mode=True))
    response = client.post(
        "/api/models",
        json={
            "name": "Iris classifier",
            "version": "1.0.0",
            "framework": "custom",
            "image": "example/iris:1.0.0",
        },
    )
    assert response.status_code == 409
    assert "reserved" in response.json()["detail"].lower()
    assert client.post("/api/models/example").status_code == 201


def test_legacy_identity_collision_returns_conflict_before_creating_artifact(tmp_path):
    from backend.app.store import Store

    Store(tmp_path).add_model(
        {
            "id": "external-model",
            "name": "Iris classifier",
            "version": "1.0.0",
            "framework": "custom",
            "kind": "container",
            "image": "example/iris:1.0.0",
        }
    )
    client = TestClient(create_app(data_dir=tmp_path, test_mode=True))
    assert client.post("/api/models/example").status_code == 409
    assert not (tmp_path / "models" / "iris-classifier.joblib").exists()
