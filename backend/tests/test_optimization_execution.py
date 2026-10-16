"""Exercise one-shot changes against controlled external cluster/HTTP adapters."""

import importlib
import json
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import ProtocolError

from backend.model_operator.spec import choose_target
from backend.model_operator.workload import render_workload
from backend.tests.test_monitoring_observations import cluster as cluster
from backend.tests.test_monitoring_observations import monitoring as monitoring
from backend.tests.test_monitoring_observations import prom_client
from backend.tests.test_monitoring_observations import responses as responses
from backend.tests.test_optimization_advisor import advisor as advisor
from backend.tests.test_optimization_advisor import provider as provider
from scripts.optimization.advisor import Decision, RecommendationUnavailable
from scripts.optimization.policy import candidates, validate_snapshot

STAMP = "mlops.strawhats.dev/last-adjustment-at"
RUN = "mlops.strawhats.dev/last-adjustment-id"


@pytest.fixture
def execution():
    return importlib.import_module("scripts.optimization.execution")


@pytest.fixture
def harness(monkeypatch, cluster, monitoring, responses, tmp_path):
    class Clock:
        seconds = 0.0
        base = datetime.now(timezone.utc)

        def now(self):
            return self.base + timedelta(seconds=self.seconds)

        def monotonic(self):
            return self.seconds

        def sleep(self, seconds):
            self.seconds += seconds

    clock = Clock()
    cluster.data["metadata"].update(resourceVersion="21", annotations={"owner": "keep"})
    cluster.data["spec"]["extra"] = "keep"
    pods = monitoring.client.CoreV1Api(cluster.sdk).list_namespaced_pod(
        monitoring.NAMESPACE, label_selector="mlops.strawhats.dev/owner-uid=owner-uid"
    )
    for expression, value in ((monitoring.CPU, 0.01), (monitoring.MEMORY, 60 * 2**20)):
        for row in responses["queries"][expression]["result"]:
            row["value"][1] = str(value)
    h = SimpleNamespace(
        cluster=cluster,
        clock=clock,
        evidence=tmp_path / "evidence",
        calls=0,
        writes=[],
        outcomes=[],
        before_decision=None,
        patch_error=None,
        after_patch=None,
        prediction=None,
        endpoints=True,
        timeouts=[],
    )

    def resource(**kwargs):
        h.timeouts.append(kwargs.get("request_timeout"))
        return deepcopy(cluster.data)

    def patch(group, version, namespace, plural, name, *, body, **kwargs):
        assert (group, version, namespace, plural, name) == (
            "mlops.strawhats.dev",
            "v1alpha1",
            cluster.data["metadata"]["namespace"],
            "modeldeployments",
            "iris",
        )
        h.writes.append(deepcopy(body))
        if h.patch_error:
            raise h.patch_error
        assert body["metadata"]["uid"] == cluster.data["metadata"]["uid"]
        if (
            body["metadata"]["resourceVersion"]
            != cluster.data["metadata"]["resourceVersion"]
        ):
            raise ApiException(409)
        cluster.data["spec"].update(body["spec"])
        cluster.data["metadata"]["generation"] += 1
        cluster.data["metadata"]["resourceVersion"] = str(
            int(cluster.data["metadata"]["resourceVersion"]) + 1
        )
        cluster.data["metadata"]["annotations"].update(
            body["metadata"].get("annotations", {})
        )
        outcome = h.outcomes.pop(0) if h.outcomes else "Ready"
        cluster.data["status"].update(phase=outcome)
        if outcome != "Pending":
            cluster.data["status"]["observedGeneration"] = cluster.data["metadata"][
                "generation"
            ]
        accepted = deepcopy(cluster.data)
        if h.after_patch:
            h.after_patch()
        return accepted

    cluster.resource = resource
    cluster.controller_ready = lambda **kwargs: True
    cluster.custom = SimpleNamespace(patch_namespaced_custom_object=patch)

    def list_pods(namespace, **kwargs):
        assert namespace == cluster.data["metadata"]["namespace"]
        h.timeouts.append(kwargs.get("_request_timeout"))
        result = cluster.sdk.sanitize_for_serialization(pods)
        template, _ = render_workload(cluster.data, choose_target(cluster.data))
        resources = template["spec"]["template"]["spec"]["containers"][0]["resources"]
        for pod in result["items"]:
            pod["spec"]["containers"][0]["resources"] = resources
        return result

    def service(*args, **kwargs):
        h.timeouts.append(kwargs.get("_request_timeout"))
        return render_workload(cluster.data, choose_target(cluster.data))[1]

    def endpoints(namespace, **kwargs):
        assert namespace == cluster.data["metadata"]["namespace"]
        h.timeouts.append(kwargs.get("_request_timeout"))
        return {
            "items": [
                {
                    "metadata": {
                        "namespace": namespace,
                        "labels": {"kubernetes.io/service-name": "iris"},
                    },
                    "ports": [{"name": "http", "port": 8000, "protocol": "TCP"}],
                    "endpoints": [
                        {
                            "conditions": {"ready": True},
                            "targetRef": {
                                "kind": "Pod",
                                "namespace": namespace,
                                "name": pod.metadata.name,
                                "uid": pod.metadata.uid,
                            },
                        }
                        for pod in pods.items
                    ]
                    if h.endpoints
                    else [],
                }
            ]
        }

    def inference(path, method, **kwargs):
        assert path == "/api/v1/namespaces/{namespace}/services/{name}/proxy/{path}"
        assert method == "POST"
        assert kwargs["path_params"] == {
            "namespace": cluster.data["metadata"]["namespace"],
            "name": "iris:8000",
            "path": "predict",
        }
        assert kwargs["body"] == {"features": [5.1, 3.5, 1.4, 0.2]}
        h.timeouts.append(kwargs["_request_timeout"])
        if h.prediction:
            return h.prediction()
        return {
            "label": "setosa",
            "confidence": 0.98,
            "probabilities": {"setosa": 0.98, "versicolor": 0.01, "virginica": 0.01},
            "latency_ms": 1.2,
        }

    monkeypatch.setattr(
        monitoring.client,
        "CoreV1Api",
        lambda sdk: SimpleNamespace(
            list_namespaced_pod=list_pods, read_namespaced_service=service
        ),
    )
    monkeypatch.setattr(
        monitoring.client,
        "DiscoveryV1Api",
        lambda sdk: SimpleNamespace(list_namespaced_endpoint_slice=endpoints),
    )
    monkeypatch.setattr(cluster.sdk, "call_api", inference)

    def advise(*args, **kwargs):
        h.calls += 1
        if h.before_decision:
            return h.before_decision()
        return Decision(action="decrease_cpu", reason="Measured low CPU with headroom.")

    def run(execution, apply=True, key="private-test-key", **options):
        with prom_client(responses) as prom:
            return execution.run_adjustment(
                cluster,
                prom,
                apply=apply,
                key=key,
                model="gpt-4.1-mini",
                evidence_dir=h.evidence,
                advisor=advise,
                now=clock.now,
                monotonic=clock.monotonic,
                wait=clock.sleep,
                **options,
            )

    h.run, h.pods, h.responses = run, pods, responses
    return h


def test_preview_reobserves_but_never_writes(execution, harness):
    result = harness.run(execution, apply=False)
    assert result["state"] == "preview" and result["action"] == "decrease_cpu"
    assert harness.calls == 1 and harness.writes == []
    assert harness.cluster.data["metadata"]["annotations"] == {"owner": "keep"}


@pytest.mark.parametrize("path", ["hold", "no_candidates", "no_key", "cooldown"])
def test_non_applying_paths(execution, harness, path):
    h = harness
    if path == "hold":
        h.before_decision = lambda: Decision(action="hold", reason="Uncertain.")
    elif path == "no_candidates":
        h.cluster.data["spec"].update(cpuMillicores=100, memoryMi=128)
    elif path == "cooldown":
        h.cluster.data["metadata"]["annotations"].update(
            {STAMP: h.clock.now().isoformat(), RUN: "previous"}
        )
    result = h.run(execution, key=None if path == "no_key" else "test")
    assert result["state"] == ("unavailable" if path == "no_key" else "held")
    assert h.writes == [] and h.calls == (1 if path == "hold" else 0)


@pytest.mark.parametrize(
    "annotations",
    [
        {STAMP: "not-a-date", RUN: "a"},
        {STAMP: "2026-10-15T01:00:00", RUN: "a"},
        {STAMP: "2999-01-01T00:00:00+00:00", RUN: "a"},
        {RUN: "a"},
        {STAMP: "2020-01-01T00:00:00Z"},
    ],
)
def test_malformed_cooldown_fails_before_provider(execution, harness, annotations):
    harness.cluster.data["metadata"]["annotations"].update(annotations)
    assert harness.run(execution)["state"] == "unavailable"
    assert harness.calls == 0 and harness.writes == []


@pytest.mark.parametrize(
    "change", ["uid", "spec", "pod_uid", "drift", "stale", "slow", "alert"]
)
def test_changed_snapshot_discards_decision(execution, harness, monitoring, change):
    h = harness

    def changed():
        if change == "uid":
            h.cluster.data["metadata"]["uid"] = "replacement"
        elif change == "spec":
            h.cluster.data["spec"]["memoryMi"] = 512
            h.cluster.data["metadata"]["generation"] += 1
        elif change == "pod_uid":
            h.pods.items[0].metadata.uid = "replacement"
        elif change == "slow":
            h.clock.seconds = 61
        elif change == "alert":
            h.responses["alerts"]["alerts"] = [
                {
                    "state": "firing",
                    "labels": {
                        "alertname": monitoring.ALERT_NAME,
                        "label": "setosa",
                        "severity": "warning",
                    },
                }
            ]
        else:
            for row in h.responses["queries"][monitoring.CPU]["result"]:
                row["value"] = (
                    [0, "0.01"]
                    if change == "stale"
                    else [h.clock.base.timestamp(), "0.2"]
                )
        return Decision(action="decrease_cpu", reason="Originally eligible.")

    h.before_decision = changed
    assert h.run(execution)["state"] == "unavailable"
    assert h.calls == 1 and h.writes == []


def test_provider_failure_has_no_write_or_secret_output(execution, harness):
    def fail():
        raise RecommendationUnavailable("Provider unavailable.")

    harness.before_decision = fail
    result = harness.run(execution)
    assert (
        result["state"] == "unavailable" and harness.calls == 1 and harness.writes == []
    )
    assert "private-test-key" not in json.dumps(result)


def test_provider_credit_failure_keeps_the_sanitized_actionable_reason(
    execution, harness
):
    reason = "OpenAI API credits or project allowance are exhausted."

    def fail():
        raise RecommendationUnavailable(reason)

    harness.before_decision = fail
    result = harness.run(execution)
    assert result["state"] == "unavailable" and result["message"] == reason
    assert harness.calls == 1 and harness.writes == []


@pytest.mark.parametrize(
    "outcome",
    [
        {"finish_reason": "length"},
        {"finish_reason": "content_filter"},
        {"empty_choices": True},
    ],
)
def test_sdk_termination_returns_unavailable_evidence_without_writes(
    execution, harness, monitoring, advisor, provider, outcome
):
    h = harness
    provider[0].update(outcome)

    def actual_advisor():
        with prom_client(h.responses) as prom:
            snapshot = validate_snapshot(
                h.cluster.resource(), monitoring.observe(h.cluster, prom), h.clock.now()
            )
        return advisor.recommend(
            snapshot,
            candidates(snapshot),
            key="test-only-placeholder",
            model="gpt-4.1-mini",
        )

    h.before_decision = actual_advisor
    result = h.run(execution)
    assert result["state"] == "unavailable" and h.writes == []
    assert h.calls == len(provider[1]) == 1
    saved = json.loads(next(h.evidence.glob("*.json")).read_text())
    assert saved[
        "state"
    ] == "unavailable" and "private-provider-detail" not in json.dumps(saved)


def test_applied_patch_and_evidence_preserve_prior_settings(execution, harness):
    h = harness
    result = h.run(execution)
    assert (
        result["state"] == "applied"
        and result["readiness"]["prediction"]["label"] == "setosa"
    )
    assert h.calls == 1 and len(h.writes) == 1
    body = h.writes[0]
    assert (
        body["metadata"]["resourceVersion"] == "21"
        and body["metadata"]["uid"] == "owner-uid"
    )
    assert set(body["metadata"]["annotations"]) == {STAMP, RUN}
    assert body["spec"]["cpuMillicores"] == 100 and body["spec"]["memoryMi"] == 256
    assert h.cluster.data["spec"]["extra"] == "keep"
    assert h.cluster.data["metadata"]["annotations"]["owner"] == "keep"
    saved = json.loads(next(h.evidence.glob("*.json")).read_text())
    assert saved["before"]["cpuMillicores"] == 250 and saved["state"] == "applied"
    assert "private-test-key" not in json.dumps(saved)


def test_rollback_uses_last_ready_effective_spec(execution, harness):
    h = harness
    h.cluster.data["status"]["lastReadySpec"] = choose_target(h.cluster.data).to_spec()
    h.cluster.data["spec"].update(mode="Rollback", cpuMillicores=500, memoryMi=512)
    result = h.run(execution)
    assert result["state"] == "applied"
    assert (
        h.writes[0]["spec"]["mode"] == "Deploy"
        and h.writes[0]["spec"]["memoryMi"] == 256
    )


@pytest.mark.parametrize(
    "status,state",
    [
        (409, "unavailable"),
        (403, "unavailable"),
        (422, "unavailable"),
        (500, "unresolved"),
        (0, "unresolved"),
    ],
)
def test_patch_rejection_or_uncertain_outcome_never_retries(
    execution, harness, status, state
):
    harness.patch_error = ApiException(status)
    assert harness.run(execution)["state"] == state
    assert len(harness.writes) == 1 and harness.calls == 1


def test_connection_drop_during_patch_never_blindly_restores(execution, harness):
    accepted_patch = harness.cluster.custom.patch_namespaced_custom_object

    def lost_response(*args, **kwargs):
        accepted_patch(*args, **kwargs)
        raise ProtocolError("response lost after acceptance")

    harness.cluster.custom.patch_namespaced_custom_object = lost_response
    assert harness.run(execution)["state"] == "unresolved"
    assert len(harness.writes) == 1
    assert harness.cluster.data["spec"]["cpuMillicores"] == 100


@pytest.mark.parametrize("failure", ["Failed", "Pending", "endpoints", "prediction"])
def test_failed_rollout_restores_once_and_verifies_service(execution, harness, failure):
    h = harness
    h.outcomes = [failure if failure in ("Failed", "Pending") else "Ready", "Ready"]
    if failure == "endpoints":
        h.endpoints = False
        h.after_patch = lambda: setattr(h, "endpoints", len(h.writes) == 2)
    elif failure == "prediction":
        h.prediction = lambda: (
            {"label": "setosa"}
            if len(h.writes) == 1
            else {
                "label": "setosa",
                "confidence": 1.0,
                "probabilities": {"setosa": 1.0, "versicolor": 0.0, "virginica": 0.0},
                "latency_ms": 2.0,
            }
        )
    result = h.run(execution)
    assert (
        result["state"] == "restored"
        and result["recovery"]["prediction"]["label"] == "setosa"
    )
    assert len(h.writes) == 2 and h.calls == 1
    assert h.writes[1]["spec"]["cpuMillicores"] == 250
    assert h.writes[1]["metadata"]["resourceVersion"] == "22"
    assert (
        h.writes[1]["metadata"]["annotations"] == h.writes[0]["metadata"]["annotations"]
    )
    assert h.clock.seconds <= 180


@pytest.mark.parametrize("change", ["uid", "generation", "spec", "run"])
def test_superseded_revision_never_restores(execution, harness, change):
    h = harness
    h.outcomes = ["Failed"]

    def supersede():
        if change == "spec":
            h.cluster.data["spec"]["replicas"] = 1
        elif change == "run":
            h.cluster.data["metadata"]["annotations"][RUN] = "other"
        else:
            h.cluster.data["metadata"][change] = (
                "replacement" if change == "uid" else 10
            )

    h.after_patch = supersede
    assert h.run(execution)["state"] == "unresolved" and len(h.writes) == 1


def test_recovery_has_separate_bounded_deadline(execution, harness):
    harness.outcomes = ["Pending", "Pending"]
    assert harness.run(execution)["state"] == "unresolved"
    assert len(harness.writes) == 2 and harness.clock.seconds == 360


def test_cooldown_blocks_second_invocation(execution, harness):
    assert harness.run(execution)["state"] == "applied"
    assert harness.run(execution)["state"] == "held"
    assert len(harness.writes) == 1 and harness.calls == 1


@pytest.mark.parametrize("after_acceptance", [False, True])
def test_evidence_failure_respects_mutation_boundary(
    execution, harness, monkeypatch, after_acceptance
):
    real = execution.save_evidence
    saves = 0

    def save(*args, **kwargs):
        nonlocal saves
        saves += 1
        if not after_acceptance or saves > 1:
            raise OSError("disk full")
        return real(*args, **kwargs)

    monkeypatch.setattr(execution, "save_evidence", save)
    result = harness.run(execution)
    assert result["state"] == ("applied" if after_acceptance else "unavailable")
    assert len(harness.writes) == (1 if after_acceptance else 0)


def test_evidence_failure_after_patch_still_recovers(execution, harness, monkeypatch):
    real = execution.save_evidence
    harness.outcomes = ["Failed", "Ready"]

    def save(path, payload):
        if harness.writes:
            raise OSError("disk full")
        return real(path, payload)

    monkeypatch.setattr(execution, "save_evidence", save)
    assert harness.run(execution)["state"] == "restored" and len(harness.writes) == 2


def test_recovery_conflict_stops_after_one_restoration_attempt(execution, harness):
    h = harness
    h.outcomes = ["Failed"]
    h.after_patch = lambda: setattr(h, "patch_error", ApiException(409))
    assert h.run(execution)["state"] == "unresolved"
    assert len(h.writes) == 2 and h.calls == 1


def test_connection_lost_after_acceptance_does_not_restore(execution, harness):
    def disconnected(**kwargs):
        raise ProtocolError("offline")

    harness.after_patch = lambda: setattr(harness.cluster, "resource", disconnected)
    assert harness.run(execution)["state"] == "unresolved" and len(harness.writes) == 1


def test_fresh_resource_version_conflict_cannot_overwrite_other_write(
    execution, harness
):
    patch = harness.cluster.custom.patch_namespaced_custom_object

    def raced(*args, **kwargs):
        harness.cluster.data["metadata"]["resourceVersion"] = "22"
        return patch(*args, **kwargs)

    harness.cluster.custom.patch_namespaced_custom_object = raced
    assert harness.run(execution)["state"] == "unavailable"
    assert (
        len(harness.writes) == 1
        and harness.cluster.data["spec"]["cpuMillicores"] == 250
    )


def test_missing_cluster_has_zero_provider_calls(execution, harness):
    harness.cluster.resource = lambda **kwargs: None
    assert harness.run(execution)["state"] == "unavailable"
    assert harness.calls == 0 and harness.writes == []


def test_invalid_initial_metrics_have_zero_provider_calls(
    execution, harness, monitoring
):
    harness.responses["queries"][monitoring.MEMORY]["result"].pop()
    assert harness.run(execution)["state"] == "unavailable"
    assert harness.calls == 0 and harness.writes == []


def test_each_rollout_request_uses_remaining_deadline(execution, harness):
    h = harness

    def controller(**kwargs):
        h.timeouts.append(kwargs.get("request_timeout"))
        if h.writes and h.clock.seconds == 0:
            h.clock.seconds = 179
        return True

    h.cluster.controller_ready = controller
    assert h.run(execution)["state"] == "applied"
    assert 1.0 in h.timeouts
    assert h.timeouts[-1] == 1.0 and h.clock.seconds == 179


@pytest.fixture
def command():
    return importlib.import_module("scripts.optimization.adjust_iris")


def test_cli_help_and_unknown_flags_never_connect(command, monkeypatch):
    monkeypatch.setattr(
        command, "connect_local_cluster", lambda: pytest.fail("must not connect")
    )
    with pytest.raises(SystemExit) as help_exit:
        command.main(["--help"])
    assert help_exit.value.code == 0
    with pytest.raises(SystemExit) as unsupported:
        command.main(["--namespace", "other"])
    assert unsupported.value.code == 2


@pytest.mark.parametrize("apply", [False, True])
def test_cli_fixed_clients_key_loading_and_no_transport_retry(
    command, monkeypatch, capsys, tmp_path, apply
):
    sdk = SimpleNamespace(
        configuration=SimpleNamespace(),
        rest_client=SimpleNamespace(
            pool_manager=SimpleNamespace(connection_pool_kw={})
        ),
    )
    loaded = []
    monkeypatch.setattr(
        command, "load_dotenv", lambda path, **kwargs: loaded.append((path, kwargs))
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-private-value")
    monkeypatch.setenv("OPENAI_MODEL", "configured-model")

    @contextmanager
    def connection():
        yield SimpleNamespace(sdk=sdk)

    monkeypatch.setattr(command, "connect_local_cluster", connection)
    original_client = command.httpx.Client

    def client(**kwargs):
        assert kwargs == {
            "base_url": "http://127.0.0.1:9090",
            "trust_env": False,
            "follow_redirects": False,
            "timeout": 5,
        }
        return original_client(**kwargs)

    monkeypatch.setattr(command.httpx, "Client", client)

    def run(cluster, prom, **kwargs):
        assert (
            kwargs["apply"] is apply
            and kwargs["key"] == "test-private-value"
            and kwargs["model"] == "configured-model"
        )
        assert kwargs["evidence_dir"] == command.ROOT / ".local" / "optimization"
        assert (
            sdk.configuration.retries.total == 0
            and sdk.configuration.retries.redirect == 0
        )
        assert sdk.rest_client.pool_manager.connection_pool_kw["retries"].total == 0
        return {"state": "applied" if apply else "preview", "action": "decrease_cpu"}

    monkeypatch.setattr(command, "run_adjustment", run)
    assert command.main(["--apply"] if apply else []) == 0
    assert loaded == [(command.ROOT / ".env.local", {"override": False})]
    assert "test-private-value" not in capsys.readouterr().out


@pytest.mark.parametrize("state", ["restored", "unavailable", "unresolved"])
def test_cli_unsuccessful_states_exit_nonzero(command, harness, monkeypatch, state):
    @contextmanager
    def connection():
        yield harness.cluster

    monkeypatch.setattr(command, "connect_local_cluster", connection)
    monkeypatch.setattr(command, "run_adjustment", lambda *a, **k: {"state": state})
    assert command.main([]) == 1
