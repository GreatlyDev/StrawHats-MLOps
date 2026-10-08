from fastapi.testclient import TestClient

from backend.serve_model import create_model_service


def test_model_container_serves_a_real_prediction_and_prometheus_metrics():
    with TestClient(create_model_service()) as client:
        assert client.get("/health").status_code == 200
        response = client.post("/predict", json={"features": [5.1, 3.5, 1.4, 0.2]})
        assert response.status_code == 200
        assert response.json()["label"] == "setosa"
        assert response.json()["confidence"] > 0.8
        assert "strawhats_model_predictions_total 1.0" in client.get("/metrics").text


def test_model_container_rejects_invalid_measurements():
    with TestClient(create_model_service()) as client:
        assert (
            client.post("/predict", json={"features": [0, 3.5, 1.4, 0.2]}).status_code
            == 422
        )
