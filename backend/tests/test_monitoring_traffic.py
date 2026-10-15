"""Exercise the bounded demo with only its external boundaries stubbed."""

import importlib
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from scripts.monitoring.observations import IMAGE, MonitoringError, Observation


class FakeClock:
    value = 0.0

    def now(self):
        return self.value

    def sleep(self, seconds):
        assert seconds >= 0
        self.value += seconds


@pytest.fixture
def demo(monkeypatch):
    try:
        module = importlib.import_module("scripts.monitoring.traffic")
    except ModuleNotFoundError:
        pytest.fail("The bounded traffic runner is not implemented")
    clock = FakeClock()
    data = {
        "metadata": {
            "name": "iris",
            "namespace": "strawhats-operator-local",
            "uid": "owner",
            "generation": 8,
        },
        "spec": {
            "mode": "Deploy",
            "image": IMAGE,
            "imagePullPolicy": "Never",
            "replicas": 2,
            "resources": {"cpu": "250m", "memory": "256Mi"},
        },
        "status": {
            "phase": "Ready",
            "observedGeneration": 8,
            "readyReplicas": 2,
            "image": IMAGE,
        },
    }
    cluster = SimpleNamespace(resource=lambda: data, controller_ready=lambda: True)
    normal = Observation(
        8,
        IMAGE,
        2,
        True,
        40,
        {"setosa": 1 / 3, "versicolor": 1 / 3, "virginica": 1 / 3},
        "inactive",
        0.2,
        200_000_000,
        ("iris-a", "iris-b"),
    )
    firing = replace(
        normal,
        class_shares={"setosa": 0.99, "versicolor": 0.005, "virginica": 0.005},
        alert_state="firing",
        alert_labels={
            "alertname": "IrisPredictionDistributionSkew",
            "label": "setosa",
            "severity": "warning",
        },
    )
    observations = iter(
        [replace(normal, recent_predictions=0, class_shares={}), normal, firing, normal]
    )
    monkeypatch.setattr(module, "observe", lambda *_, **kwargs: next(observations))
    calls = []

    def predict(_, resource, payload, **kwargs):
        assert resource is data
        calls.append((clock.now(), payload.features))
        label = next(
            label
            for label, vector in module.VECTORS.items()
            if vector == payload.features
        )
        return {
            "label": label,
            "confidence": 0.98,
            "probabilities": {
                key: 0.98 if key == label else 0.01 for key in module.VECTORS
            },
            "latency_ms": 2.0,
        }

    monkeypatch.setattr(module, "predict_with_service", predict)
    return SimpleNamespace(
        module=module,
        clock=clock,
        data=data,
        cluster=cluster,
        calls=calls,
        normal=normal,
        firing=firing,
        predict=predict,
    )


def run(demo):
    return demo.module.run_demo(
        demo.cluster, object(), clock=demo.clock.now, sleep=demo.clock.sleep
    )


def test_balanced_firing_recovery_and_request_spacing(demo):
    result = run(demo)
    assert result["complete"] is True
    assert result["successful_requests"] == len(demo.calls) <= 1000
    assert [row["phase"] for row in result["phases"]] == [
        "baseline",
        "firing",
        "recovery",
    ]
    assert result["generation"] == 8 and result["image"] == IMAGE
    assert result["phases"][1]["observation"]["alert_labels"]["label"] == "setosa"
    assert result["phases"][2]["observation"]["target_pods"] == ("iris-a", "iris-b")
    vectors = list(demo.module.VECTORS.values())
    assert [row[1] for row in demo.calls[:60]] == vectors * 20
    assert all(
        later[0] - earlier[0] >= 0.5
        for earlier, later in zip(demo.calls, demo.calls[1:])
    )
    assert demo.clock.now() < 600


def test_missing_alert_after_scrape_failure_is_not_recovery(demo, monkeypatch):
    states = iter(
        [
            demo.normal,
            demo.normal,
            demo.firing,
            replace(demo.normal, available=False, alert_state="unavailable"),
        ]
    )
    monkeypatch.setattr(demo.module, "observe", lambda *_, **kwargs: next(states))
    with pytest.raises(MonitoringError) as error:
        run(demo)
    assert error.value.evidence["complete"] is False
    assert [row["phase"] for row in error.value.evidence["phases"]] == [
        "baseline",
        "firing",
    ]


def test_revision_change_stops_traffic(demo, monkeypatch):
    def changed(*args, **kwargs):
        result = demo.predict(*args)
        demo.data["metadata"]["generation"] = 9
        demo.data["status"]["observedGeneration"] = 9
        return result

    monkeypatch.setattr(demo.module, "predict_with_service", changed)
    with pytest.raises(MonitoringError):
        run(demo)
    assert len(demo.calls) == 1


def test_transport_failure_stops_without_another_request(demo, monkeypatch):
    def failed(*args, **kwargs):
        demo.calls.append((demo.clock.now(), args[2].features))
        raise HTTPException(503, "Transport unavailable")

    monkeypatch.setattr(demo.module, "predict_with_service", failed)
    with pytest.raises(MonitoringError) as error:
        run(demo)
    assert len(demo.calls) == 1
    assert error.value.evidence["successful_requests"] == 0
    assert error.value.evidence["complete"] is False


def test_deadline_starts_no_operation_after_inflight_timeout(demo, monkeypatch):
    reads = []
    demo.cluster.resource = lambda: reads.append(demo.clock.now()) or demo.data

    def slow(*args, **kwargs):
        result = demo.predict(*args)
        demo.clock.value = 601
        return result

    monkeypatch.setattr(demo.module, "predict_with_service", slow)
    with pytest.raises(MonitoringError):
        run(demo)
    assert len(demo.calls) == 1
    assert all(stamp < 600 for stamp in reads)


def test_success_budget_stops_at_1000_requests(demo, monkeypatch):
    monkeypatch.setattr(demo.module, "observe", lambda *_, **kwargs: demo.normal)
    with pytest.raises(MonitoringError) as error:
        run(demo)
    assert len(demo.calls) == 1000
    assert error.value.evidence["successful_requests"] == 1000
    assert error.value.evidence["complete"] is False


def test_wrong_returned_class_stops_demo(demo, monkeypatch):
    def wrong(*args, **kwargs):
        result = demo.predict(*args)
        return {**result, "label": "virginica"}

    monkeypatch.setattr(demo.module, "predict_with_service", wrong)
    with pytest.raises(MonitoringError):
        run(demo)
    assert len(demo.calls) == 1


def test_initial_unavailable_telemetry_sends_no_predictions(demo, monkeypatch):
    monkeypatch.setattr(
        demo.module,
        "observe",
        lambda *_, **kwargs: replace(
            demo.normal, available=False, alert_state="unavailable"
        ),
    )
    with pytest.raises(MonitoringError):
        run(demo)
    assert demo.calls == []


def test_resource_replacement_with_same_generation_stops_traffic(demo, monkeypatch):
    def replaced(*args, **kwargs):
        result = demo.predict(*args, **kwargs)
        demo.data["metadata"]["uid"] = "replacement-owner"
        return result

    monkeypatch.setattr(demo.module, "predict_with_service", replaced)
    with pytest.raises(MonitoringError) as error:
        run(demo)
    assert len(demo.calls) == 1
    assert error.value.evidence["complete"] is False


def test_deadline_between_service_get_and_prediction_post(demo, monkeypatch):
    from backend.app import local_inference

    starts = []

    def read_service(*args, **kwargs):
        starts.append(("GET", demo.clock.now()))
        demo.clock.value = 601
        return {
            "metadata": {
                "ownerReferences": [
                    {
                        "uid": "owner",
                        "kind": "ModelDeployment",
                        "name": "iris",
                        "apiVersion": "mlops.strawhats.dev/v1alpha1",
                        "controller": True,
                    }
                ]
            },
            "spec": {
                "type": "ClusterIP",
                "selector": {"mlops.strawhats.dev/owner-uid": "owner"},
                "ports": [{"port": 8000, "targetPort": "http", "protocol": "TCP"}],
            },
        }

    def post(*args, **kwargs):
        starts.append(("POST", demo.clock.now()))
        return {
            "label": "setosa",
            "confidence": 0.98,
            "probabilities": {"setosa": 0.98, "versicolor": 0.01, "virginica": 0.01},
            "latency_ms": 2.0,
        }

    demo.cluster.sdk = SimpleNamespace(
        sanitize_for_serialization=lambda value: value, call_api=post
    )
    monkeypatch.setattr(
        "kubernetes.client.CoreV1Api",
        lambda _: SimpleNamespace(read_namespaced_service=read_service),
    )

    def initial(*args, **kwargs):
        demo.clock.value = 599
        return demo.normal

    monkeypatch.setattr(demo.module, "observe", initial)
    monkeypatch.setattr(
        demo.module, "predict_with_service", local_inference.predict_with_service
    )
    with pytest.raises(MonitoringError) as error:
        run(demo)
    assert starts == [("GET", 599)]
    assert error.value.evidence["complete"] is False
