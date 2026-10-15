from fastapi.testclient import TestClient

from backend.app.main import create_app


def test_missing_summary_uses_the_existing_model_not_found_guard(tmp_path):
    with TestClient(create_app(tmp_path, test_mode=True)) as client:
        response = client.get("/api/models/missing/prediction-summary")
        assert response.status_code == 404
        assert response.json() == {"detail": "Model not found in this workspace."}


def test_registered_container_has_an_empty_workspace_summary(tmp_path):
    with TestClient(create_app(tmp_path, test_mode=True)) as client:
        model = client.post(
            "/api/models",
            json={
                "name": "Container example",
                "version": "1",
                "framework": "HTTP",
                "image": "registry.example.com/model:v1",
            },
        ).json()
        response = client.get(f"/api/models/{model['id']}/prediction-summary")
        assert response.status_code == 200
        assert response.json() == {
            "model_id": model["id"],
            "predictions": 0,
            "label_counts": {},
            "median_latency_ms": None,
            "recent_inferences": [],
        }


def test_summary_counts_only_successful_real_predictions(tmp_path):
    with TestClient(create_app(tmp_path, test_mode=True)) as client:
        assert client.post("/api/models/example").status_code == 201
        first = client.post(
            "/api/models/iris-classifier/predict",
            json={
                "features": [5.1, 3.5, 1.4, 0.2],
            },
        ).json()
        second = client.post(
            "/api/models/iris-classifier/predict",
            json={
                "features": [6.3, 3.3, 6, 2.5],
            },
        ).json()
        assert first["label"] == "setosa"
        assert second["label"] == "virginica"
        assert (
            client.post(
                "/api/models/iris-classifier/predict",
                json={
                    "features": [0, 3.3, 6, 2.5],
                },
            ).status_code
            == 422
        )
        response = client.get("/api/models/iris-classifier/prediction-summary")
        assert response.status_code == 200
        summary = response.json()
        assert summary["model_id"] == "iris-classifier"
        assert summary["predictions"] == 2
        assert summary["label_counts"] == {"setosa": 1, "virginica": 1}
        assert [r["label"] for r in summary["recent_inferences"]] == [
            "setosa",
            "virginica",
        ]
        assert (
            summary["median_latency_ms"]
            == (first["latency_ms"] + second["latency_ms"]) / 2
        )
        assert all(
            set(r) == {"model_id", "label", "latency_ms", "created_at"}
            for r in summary["recent_inferences"]
        )


def test_summary_reads_preserve_records_and_survive_application_reopen(tmp_path):
    with TestClient(create_app(tmp_path, test_mode=True)) as client:
        assert client.post("/api/models/example").status_code == 201
        assert (
            client.post(
                "/api/models/iris-classifier/predict",
                json={
                    "features": [5.1, 3.5, 1.4, 0.2],
                },
            ).status_code
            == 200
        )
        models = client.get("/api/models").json()
        overview = client.get("/api/overview").json()
        events = client.get("/api/events").json()
        response = client.get("/api/models/iris-classifier/prediction-summary")
        assert response.status_code == 200
        summary = response.json()
        assert (
            client.get("/api/models/iris-classifier/prediction-summary").json()
            == summary
        )
        assert client.get("/api/models").json() == models
        assert client.get("/api/overview").json() == overview
        assert client.get("/api/events").json() == events
    with TestClient(create_app(tmp_path, test_mode=True)) as reopened:
        assert (
            reopened.get("/api/models/iris-classifier/prediction-summary").json()
            == summary
        )
        assert reopened.get("/api/overview").json() == overview
        assert reopened.get("/api/events").json() == events
