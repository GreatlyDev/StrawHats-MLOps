"""Runtime commands never substitute targets or bypass the common write guard."""

import importlib
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.app.iris_target import RuntimeTarget
from backend.tests.test_iris_targets import bind
from backend.tests.test_monitoring_observations import prom_client
from backend.tests.test_optimization_advisor import advisor as advisor
from backend.tests.test_optimization_advisor import provider as provider
from backend.tests.test_optimization_execution import (  # noqa: F401
    cluster as cluster,
)
from backend.tests.test_optimization_execution import (
    execution as execution,
)
from backend.tests.test_optimization_execution import (
    harness as harness,
)
from backend.tests.test_optimization_execution import (
    monitoring as monitoring,
)
from backend.tests.test_optimization_execution import (
    responses as responses,
)
from scripts.optimization.advisor import Decision, RecommendationUnavailable
from scripts.optimization.policy import candidates, validate_snapshot


@pytest.fixture(params=["eks", "rehearsal"])
def runtime(request):
    if request.param == "rehearsal":
        return RuntimeTarget(
            "rehearsal",
            "docker-desktop",
            "strawhats",
            "strawhats-monitoring",
            "iris",
            "strawhats-iris:rehearsal-39f81d1",
            "Never",
            "strawhats-model-operator",
            "strawhats-model-operator:rehearsal-39f81d1",
        )
    root = "123456789012.dkr.ecr.us-east-1.amazonaws.com/strawhats-midterm/"
    return RuntimeTarget(
        "eks",
        "arn:aws:eks:us-east-1:123456789012:cluster/strawhats-midterm",
        "strawhats",
        "strawhats-monitoring",
        "iris",
        root + "iris@sha256:" + "a" * 64,
        "IfNotPresent",
        "strawhats-model-operator",
        root + "operator@sha256:" + "b" * 64,
    )


@pytest.fixture
def tools():
    try:
        return importlib.import_module("scripts.cloud.runtime_tools")
    except ModuleNotFoundError:
        pytest.fail("Owner runtime commands are not implemented")


@pytest.mark.parametrize("operation", ["observe", "traffic", "adjust"])
def test_command_help_invalid_and_missing_args_never_connect(
    tools, monkeypatch, operation
):
    monkeypatch.setattr(
        tools, "owner_connection", lambda *a, **k: pytest.fail("Unexpected connection")
    )
    entry = importlib.import_module(
        "scripts.cloud."
        + {
            "observe": "observe_iris",
            "traffic": "traffic_iris",
            "adjust": "adjust_iris",
        }[operation]
    )
    for argv, expected in [
        (["--help"], 0),
        ([], 2),
        (["--namespace", "foreign"], 2),
        (["--target", "eks", "--receipt", "unused"], 2),
    ]:
        with pytest.raises(SystemExit) as result:
            entry.main(argv)
        assert result.value.code == expected


def args(runtime):
    return ["--target", runtime.kind, "--receipt", "runtime.json"] + (
        ["--profile", "owner", "--account", "123456789012"]
        if runtime.kind == "eks"
        else []
    )


@pytest.mark.parametrize("operation", ["observe", "traffic", "adjust"])
def test_commands_pass_only_bound_target_guard_and_private_transport(
    tools, runtime, monkeypatch, tmp_path, operation
):
    connection = SimpleNamespace(
        target=runtime, cluster=object(), check_mutation=lambda: None
    )
    monkeypatch.setattr(
        tools.RuntimeReceipt, "load", lambda p: SimpleNamespace(kind=runtime.kind)
    )

    @contextmanager
    def connect(kind, config, *, purpose="operate"):
        assert kind == runtime.kind and purpose == "operate"
        assert (
            config["EKS_RELEASE_RECEIPT"] == "runtime.json"
            if kind == "eks"
            else config["REHEARSAL_RECEIPT"] == "runtime.json"
        )
        yield connection

    monkeypatch.setattr(tools, "owner_connection", connect)
    original = tools.httpx.Client

    def client(**kwargs):
        assert kwargs == dict(
            base_url="http://127.0.0.1:9090",
            trust_env=False,
            follow_redirects=False,
            timeout=5,
        )
        return original(**kwargs)

    monkeypatch.setattr(tools.httpx, "Client", client)
    monkeypatch.setattr(tools, "ROOT", tmp_path)
    monkeypatch.setattr(tools, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("OPENAI_API_KEY", "private-test-value")

    def perform(cluster, prom, **kwargs):
        assert cluster is connection.cluster and kwargs["target"] is runtime
        if operation == "observe":
            return SimpleNamespace(available=True)
        if operation == "adjust":
            assert (
                kwargs["before_mutation"] == connection.check_mutation
                and not kwargs["apply"]
            )
            return {"state": "preview"}
        return {"complete": True}

    monkeypatch.setattr(tools, "observe", perform)
    monkeypatch.setattr(tools, "run_demo", perform)
    monkeypatch.setattr(tools, "run_adjustment", perform)
    monkeypatch.setattr(tools, "asdict", lambda v: {"available": v.available})
    assert (
        tools.command_main(
            operation,
            args(runtime) + (["--run-demo"] if operation == "traffic" else []),
        )
        == 0
    )
    assert (
        tmp_path / ".local/owner-eks" / runtime.kind / (operation + ".json")
    ).exists()


def test_copied_receipt_rejected_before_connection(tools, runtime, monkeypatch):
    monkeypatch.setattr(
        tools.RuntimeReceipt,
        "load",
        lambda p: SimpleNamespace(kind="rehearsal" if runtime.kind == "eks" else "eks"),
    )
    monkeypatch.setattr(
        tools,
        "owner_connection",
        lambda *a, **k: pytest.fail("Copied receipt connected"),
    )
    assert tools.command_main("observe", args(runtime)) == 1


@pytest.mark.parametrize(
    "failure",
    [
        "no_key",
        "hold",
        "cooldown",
        "metrics",
        "provider",
        "pod_uid",
        "generation",
        "expired",
        "slow_guard",
        "restore_guard",
        "endpoints",
        "evidence",
    ],
)
def test_nonlocal_adjustment_failure_boundaries(
    runtime, harness, execution, monkeypatch, failure
):
    h = harness
    bind(h, runtime)
    guard_calls = []

    def guard():
        guard_calls.append(len(h.writes))
        if failure in ("expired", "restore_guard"):
            raise HTTPException(503, "Approval changed")
        if failure == "slow_guard":
            h.clock.seconds += 61

    if failure == "restore_guard":
        h.outcomes = ["Failed"]

        def guard():
            guard_calls.append(len(h.writes))
            if h.writes:
                raise HTTPException(503, "Owner changed before restoration")

    if failure == "hold":
        h.before_decision = lambda: Decision(
            action="hold", reason="Controlled test decision"
        )
    if failure == "cooldown":
        h.cluster.data["metadata"]["annotations"].update(
            {
                execution.STAMP: h.clock.now().isoformat(),
                execution.RUN: "controlled-test",
            }
        )
    if failure == "metrics":
        h.responses["queries"]["iris:telemetry_available"]["result"] = []
    if failure == "provider":

        def fail():
            raise RecommendationUnavailable("Controlled provider failure")

        h.before_decision = fail
    if failure in ("pod_uid", "generation"):

        def changed():
            if failure == "pod_uid":
                h.pods.items[0].metadata.uid = "replacement"
            else:
                h.cluster.data["metadata"]["generation"] += 1
            return Decision(action="decrease_cpu", reason="Controlled test")

        h.before_decision = changed
    if failure == "endpoints":
        h.endpoints = False
    if failure == "evidence":
        monkeypatch.setattr(
            execution,
            "save_evidence",
            lambda *a: (_ for _ in ()).throw(OSError("disk full")),
        )
    result = h.run(
        execution,
        target=runtime,
        before_mutation=guard,
        key=None if failure == "no_key" else "test-only",
    )
    expected = 1 if failure == "restore_guard" else 2 if failure == "endpoints" else 0
    assert len(h.writes) == expected
    assert h.calls <= 1 and result["state"] in (
        "held",
        "unavailable",
        "unresolved",
        "restored",
    )


def test_nonlocal_source_freshness_and_independent_recovery_deadlines(
    runtime, harness, execution
):
    h = harness
    bind(h, runtime)
    rows = h.responses["queries"][
        'timestamp(process_cpu_seconds_total{job="iris-model"})'
    ]["result"]
    fresh = rows[0]["value"][1]
    rows[0]["value"][1] = str(float(fresh) - 60)
    assert (
        h.run(execution, target=runtime, before_mutation=lambda: None)["state"]
        == "unavailable"
    )
    assert h.calls == 0 and h.writes == []
    rows[0]["value"][1] = fresh
    h.outcomes = ["Pending", "Pending"]
    assert (
        h.run(execution, target=runtime, before_mutation=lambda: None)["state"]
        == "unresolved"
    )
    assert len(h.writes) == 2 and h.clock.seconds == 360


def test_nonlocal_real_langchain_transport_and_owner_change(
    runtime, harness, execution, monitoring, advisor, provider
):
    h = harness
    bind(h, runtime)

    def recommend():
        with prom_client(h.responses) as prom:
            snapshot = validate_snapshot(
                h.cluster.resource(),
                monitoring.observe(h.cluster, prom, target=runtime),
                h.clock.now(),
                target=runtime,
            )
        return advisor.recommend(
            snapshot, candidates(snapshot), key="test-only", model="gpt-4.1-mini"
        )

    h.before_decision = recommend

    def changed_owner():
        raise HTTPException(503, "Controlled owner change after provider reply")

    assert (
        h.run(execution, target=runtime, before_mutation=changed_owner)["state"]
        == "unavailable"
    )
    assert len(provider[1]) == 1 and h.calls == 1 and h.writes == []
