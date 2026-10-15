import importlib
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from kubernetes import client

IMAGE = "strawhats-iris:local-monitoring"


@pytest.fixture
def monitoring():
    try:
        return importlib.import_module("scripts.monitoring.observations")
    except ModuleNotFoundError:
        pytest.fail("Local monitoring observation helper is not implemented")


@pytest.fixture
def cluster(monkeypatch, monitoring):
    resource = {
        "apiVersion": "mlops.strawhats.dev/v1alpha1",
        "kind": "ModelDeployment",
        "metadata": {
            "name": "iris",
            "namespace": "strawhats-operator-local",
            "uid": "owner-uid",
            "generation": 8,
        },
        "spec": {
            "mode": "Deploy",
            "image": IMAGE,
            "imagePullPolicy": "Never",
            "replicas": 2,
            "cpuMillicores": 250,
            "memoryMi": 256,
        },
        "status": {
            "phase": "Ready",
            "observedGeneration": 8,
            "readyReplicas": 2,
            "image": IMAGE,
        },
    }
    pods = client.V1PodList(
        items=[
            client.V1Pod(
                metadata=client.V1ObjectMeta(
                    name=f"iris-abc-{name}",
                    namespace="strawhats-operator-local",
                    uid=f"pod-{name}",
                    labels={"mlops.strawhats.dev/owner-uid": "owner-uid"},
                ),
                spec=client.V1PodSpec(
                    containers=[
                        client.V1Container(
                            name="iris",
                            image=IMAGE,
                            ports=[
                                client.V1ContainerPort(name="http", container_port=8000)
                            ],
                        )
                    ]
                ),
                status=client.V1PodStatus(
                    phase="Running",
                    conditions=[client.V1PodCondition(type="Ready", status="True")],
                ),
            )
            for name in ("a", "b")
        ]
    )

    def list_pods(namespace, *, label_selector, **kwargs):
        assert namespace == "strawhats-operator-local"
        assert label_selector == "mlops.strawhats.dev/owner-uid=owner-uid"
        return pods

    monkeypatch.setattr(
        monitoring.client,
        "CoreV1Api",
        lambda sdk: SimpleNamespace(list_namespaced_pod=list_pods),
    )
    with client.ApiClient() as sdk:
        value = SimpleNamespace(
            sdk=sdk,
            data=resource,
            resource=lambda: deepcopy(resource),
            controller_ready=lambda: True,
        )
        yield value


@pytest.fixture
def responses():
    now = datetime.now(timezone.utc)

    def vector(rows):
        return {
            "resultType": "vector",
            "result": [
                {"metric": labels, "value": [now.timestamp(), str(value)]}
                for labels, value in rows
            ],
        }

    targets = [
        {
            "discoveredLabels": {},
            "labels": {
                "job": "iris-model",
                "instance": name,
                "namespace": "strawhats-operator-local",
                "pod": f"iris-abc-{name}",
                "image": IMAGE,
            },
            "scrapePool": "iris-model",
            "scrapeUrl": f"http://10.0.0.{index}:8000/metrics",
            "globalUrl": f"http://10.0.0.{index}:8000/metrics",
            "lastError": "",
            "lastScrape": now.isoformat(),
            "lastScrapeDuration": 0.01,
            "health": "up",
            "scrapeInterval": "10s",
            "scrapeTimeout": "5s",
        }
        for index, name in enumerate(("a", "b"), 1)
    ]
    queries = {
        "iris:telemetry_available": vector([({}, 1)]),
        "iris:predictions:increase2m": vector([({}, 40)]),
        "iris:class_share:ratio2m": vector(
            [
                ({"label": "setosa"}, 0.5),
                ({"label": "versicolor"}, 0.25),
                ({"label": "virginica"}, 0.25),
            ]
        ),
        'count by(instance)(strawhats_model_prediction_class_total{job="iris-model",label=~"setosa|versicolor|virginica"})': vector(
            [({"instance": "a"}, 3), ({"instance": "b"}, 3)]
        ),
        'rate(process_cpu_seconds_total{job="iris-model"}[2m])': vector(
            [({"instance": "a"}, 0.1), ({"instance": "b"}, 0.15)]
        ),
        'process_resident_memory_bytes{job="iris-model"}': vector(
            [({"instance": "a"}, 134217728), ({"instance": "b"}, 134217728)]
        ),
    }
    return {
        "targets": {"activeTargets": targets, "droppedTargets": []},
        "rules": {
            "groups": [
                {
                    "name": "local-iris",
                    "file": "/etc/prometheus/iris-alerts.yaml",
                    "interval": 10,
                    "evaluationTime": 0.001,
                    "lastEvaluation": now.isoformat(),
                    "rules": [
                        {
                            "name": "IrisPredictionDistributionSkew",
                            "type": "alerting",
                            "health": "ok",
                            "lastError": "",
                            "state": "inactive",
                            "query": "iris:class_share:ratio2m > 0.90",
                            "duration": 30,
                            "keepFiringFor": 0,
                            "labels": {"severity": "warning"},
                            "annotations": {},
                            "alerts": [],
                        }
                    ],
                }
            ]
        },
        "alerts": {"alerts": []},
        "queries": queries,
    }


def prom_client(responses):
    def respond(request):
        if request.url.path == "/api/v1/query":
            data = responses["queries"][request.url.params["query"]]
        else:
            data = responses[request.url.path.rsplit("/", 1)[-1]]
        return httpx.Response(200, json={"status": "success", "data": data})

    return httpx.Client(
        base_url="http://127.0.0.1:9090",
        transport=httpx.MockTransport(respond),
        trust_env=False,
    )


def test_observation_matches_two_owned_targets_and_real_metric_values(
    monitoring, cluster, responses
):
    with prom_client(responses) as prom:
        result = monitoring.observe(cluster, prom)
    assert result.available is True
    assert result.generation == 8 and result.image == IMAGE
    assert result.target_count == 2
    assert result.target_pods == ("iris-abc-a", "iris-abc-b")
    assert result.recent_predictions == 40
    assert result.class_shares == {"setosa": 0.5, "versicolor": 0.25, "virginica": 0.25}
    assert result.process_cpu_cores == 0.25
    assert result.resident_memory_bytes == 268435456
    assert result.alert_state == "inactive"


@pytest.mark.parametrize(
    "fault",
    [
        "missing_class",
        "missing_target",
        "wrong_image",
        "failed_scrape",
        "stale_scrape",
        "missing_rule",
        "bad_rule",
        "bad_cpu",
        "bad_memory",
        "bad_share",
    ],
)
def test_incomplete_telemetry_never_claims_recovery(
    monitoring, cluster, responses, fault
):
    if fault == "missing_class":
        responses["queries"][
            'count by(instance)(strawhats_model_prediction_class_total{job="iris-model",label=~"setosa|versicolor|virginica"})'
        ]["result"][1]["value"][1] = "2"
    elif fault == "missing_target":
        responses["targets"]["activeTargets"].pop()
    elif fault == "wrong_image":
        responses["targets"]["activeTargets"][0]["labels"]["image"] = (
            "strawhats-iris:older"
        )
    elif fault == "failed_scrape":
        responses["targets"]["activeTargets"][0]["health"] = "down"
    elif fault == "stale_scrape":
        responses["targets"]["activeTargets"][0]["lastScrape"] = (
            datetime.now(timezone.utc) - timedelta(minutes=5)
        ).isoformat()
    elif fault == "missing_rule":
        responses["rules"]["groups"] = []
    elif fault == "bad_rule":
        responses["rules"]["groups"][0]["rules"][0]["health"] = "err"
    elif fault == "bad_cpu":
        responses["queries"]['rate(process_cpu_seconds_total{job="iris-model"}[2m])'][
            "result"
        ][0]["value"][1] = "NaN"
    elif fault == "bad_memory":
        responses["queries"]['process_resident_memory_bytes{job="iris-model"}'][
            "result"
        ][0]["value"][1] = "Infinity"
    else:
        responses["queries"]["iris:class_share:ratio2m"]["result"][0]["value"][1] = (
            "NaN"
        )
    with prom_client(responses) as prom:
        result = monitoring.observe(cluster, prom)
    assert result.available is False
    assert result.alert_state == "unavailable"


def test_no_predictions_keep_unmeasured_shares_unavailable(
    monitoring, cluster, responses
):
    responses["queries"]["iris:predictions:increase2m"]["result"][0]["value"][1] = "0"
    for row in responses["queries"]["iris:class_share:ratio2m"]["result"]:
        row["value"][1] = "NaN"
    with prom_client(responses) as prom:
        result = monitoring.observe(cluster, prom)
    assert result.available is True
    assert result.recent_predictions == 0
    assert result.class_shares == {}


@pytest.mark.parametrize(
    "change", ["unobserved", "unready", "older_image", "different_generation"]
)
def test_prediction_snapshot_refuses_noncurrent_workloads(monitoring, cluster, change):
    if change == "unobserved":
        cluster.data["status"]["observedGeneration"] = 7
    elif change == "unready":
        cluster.data["status"]["readyReplicas"] = 0
    elif change == "older_image":
        cluster.data["spec"]["image"] = "strawhats-iris:milestone-one"
    else:
        cluster.data["metadata"]["generation"] = 9
        cluster.data["status"]["observedGeneration"] = 9
    with pytest.raises(monitoring.MonitoringError):
        monitoring.require_snapshot(cluster, expected_generation=8)


def test_prometheus_redirect_never_selects_another_host(monitoring, cluster):
    hosts = []

    def redirect(request):
        hosts.append(request.url.host)
        return httpx.Response(
            307, headers={"Location": "https://external.invalid/metrics"}
        )

    with httpx.Client(
        base_url="http://127.0.0.1:9090",
        transport=httpx.MockTransport(redirect),
        follow_redirects=True,
    ) as prom:
        with pytest.raises(monitoring.MonitoringError):
            monitoring.observe(cluster, prom)
    assert hosts == ["127.0.0.1"]


def test_firing_evidence_preserves_actual_warning_labels(
    monitoring, cluster, responses
):
    responses["alerts"]["alerts"] = [
        {
            "labels": {
                "alertname": "IrisPredictionDistributionSkew",
                "label": "setosa",
                "severity": "warning",
            },
            "annotations": {},
            "state": "firing",
            "activeAt": datetime.now(timezone.utc).isoformat(),
            "value": "0.99",
        }
    ]
    with prom_client(responses) as prom:
        result = monitoring.observe(cluster, prom)
    assert result.alert_state == "firing"
    assert result.alert_labels == {
        "alertname": "IrisPredictionDistributionSkew",
        "label": "setosa",
        "severity": "warning",
    }


def test_observation_cli_rejects_overrides_before_connecting(monitoring, monkeypatch):
    monkeypatch.setattr(
        "sys.argv", ["observations", "--url", "https://external.invalid"]
    )

    def forbidden():
        pytest.fail("Unknown CLI arguments must be rejected before cluster access")

    monkeypatch.setattr(monitoring, "connect_local_cluster", forbidden)
    with pytest.raises(SystemExit) as error:
        monitoring.main()
    assert error.value.code == 2


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"alerts": None},
        {"alerts": {}},
        {"alerts": [None]},
        {"alerts": [{"labels": None}]},
    ],
)
def test_malformed_alert_envelope_cannot_be_healthy_inactive(
    monitoring, cluster, responses, payload
):
    responses["alerts"] = payload
    with prom_client(responses) as prom, pytest.raises(monitoring.MonitoringError):
        monitoring.observe(cluster, prom)


@pytest.mark.parametrize(
    "fault",
    [
        "null_vector_row",
        "wrong_metric_type",
        "null_targets",
        "null_groups",
        "null_rules",
        "null_scrape_time",
    ],
)
def test_malformed_nested_observations_fail_closed(
    monitoring, cluster, responses, fault
):
    if fault == "null_vector_row":
        responses["queries"][monitoring.CPU]["result"][0] = None
    elif fault == "wrong_metric_type":
        responses["queries"][monitoring.CPU]["result"][0]["metric"] = []
    elif fault == "null_targets":
        responses["targets"]["activeTargets"] = None
    elif fault == "null_groups":
        responses["rules"]["groups"] = None
    elif fault == "null_rules":
        responses["rules"]["groups"][0]["rules"] = None
    else:
        responses["targets"]["activeTargets"][0]["lastScrape"] = None
    with prom_client(responses) as prom:
        try:
            result = monitoring.observe(cluster, prom)
        except monitoring.MonitoringError:
            return
    assert result.available is False and result.alert_state == "unavailable"


def test_demo_deadline_between_actual_observation_requests(
    monitoring, cluster, responses
):
    from scripts.monitoring.traffic import run_demo

    elapsed = [0.0]
    starts = []

    def respond(request):
        starts.append((request.url.path, elapsed[0]))
        elapsed[0] = 601
        key = request.url.path.rsplit("/", 1)[-1]
        data = (
            responses["queries"][request.url.params["query"]]
            if key == "query"
            else responses[key]
        )
        return httpx.Response(200, json={"status": "success", "data": data})

    with (
        httpx.Client(
            base_url=monitoring.PROMETHEUS_URL, transport=httpx.MockTransport(respond)
        ) as prom,
        pytest.raises(monitoring.MonitoringError) as error,
    ):
        run_demo(
            cluster,
            prom,
            clock=lambda: elapsed[0],
            sleep=lambda value: elapsed.__setitem__(0, elapsed[0] + value),
        )
    assert starts == [("/api/v1/targets", 0)]
    assert error.value.evidence["complete"] is False
