from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

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


def class_counts(client):
    return {
        sample.labels["label"]: sample.value
        for family in text_string_to_metric_families(client.get("/metrics").text)
        for sample in family.samples
        if sample.name == "strawhats_model_prediction_class_total"
    }


def prediction_count(client):
    return next(
        sample.value
        for family in text_string_to_metric_families(client.get("/metrics").text)
        for sample in family.samples
        if sample.name == "strawhats_model_predictions_total"
    )


def test_class_metrics_follow_actual_predictions():
    expected = dict.fromkeys(("setosa", "versicolor", "virginica"), 0)
    with TestClient(create_model_service()) as client:
        assert class_counts(client) == expected
        for features in (
            [5.1, 3.5, 1.4, 0.2],
            [6.0, 2.9, 4.5, 1.5],
            [6.3, 3.3, 6.0, 2.5],
        ):
            response = client.post("/predict", json={"features": features})
            assert response.status_code == 200
            result = response.json()
            expected[result["label"]] += 1
            assert set(result) == {"label", "confidence", "probabilities", "latency_ms"}
            assert class_counts(client) == expected
        assert sum(class_counts(client).values()) == prediction_count(client) == 3


def test_invalid_input_does_not_increment_class_metrics():
    with TestClient(create_model_service()) as client:
        assert class_counts(client) == {
            "setosa": 0,
            "versicolor": 0,
            "virginica": 0,
        }
        response = client.post("/predict", json={"features": [0, 3.5, 1.4, 0.2]})
        assert response.status_code == 422
        assert class_counts(client) == {
            "setosa": 0,
            "versicolor": 0,
            "virginica": 0,
        }
        assert prediction_count(client) == 0


def test_serving_registries_are_isolated():
    with (
        TestClient(create_model_service()) as first,
        TestClient(create_model_service()) as second,
    ):
        first.post(
            "/predict", json={"features": [5.1, 3.5, 1.4, 0.2]}
        ).raise_for_status()
        assert class_counts(first) == {"setosa": 1, "versicolor": 0, "virginica": 0}
        assert class_counts(second) == {"setosa": 0, "versicolor": 0, "virginica": 0}
        assert prediction_count(second) == 0
