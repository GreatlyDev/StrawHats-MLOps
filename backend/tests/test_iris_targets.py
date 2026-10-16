"""Runtime bindings must not leak into another namespace, image or write window."""

import importlib
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.tests.test_monitoring_observations import prom_client
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
from scripts.monitoring.observations import MonitoringError


@pytest.fixture
def targets():
    try:
        return importlib.import_module("backend.app.iris_target")
    except ModuleNotFoundError:
        pytest.fail("Explicit Iris runtime bindings are not implemented")


@pytest.fixture
def rehearsal(targets):
    return targets.RuntimeTarget(
        kind="rehearsal",
        context="docker-desktop",
        namespace="strawhats",
        monitoring_namespace="strawhats-monitoring",
        name="iris",
        image="strawhats-iris:rehearsal-39f81d1",
        image_pull_policy="Never",
        operator_name="strawhats-model-operator",
        operator_image="strawhats-model-operator:rehearsal-39f81d1",
    )


def bind(h, target):
    h.cluster.data["metadata"]["namespace"] = target.namespace
    h.cluster.data["spec"].update(
        image=target.image, imagePullPolicy=target.image_pull_policy
    )
    h.cluster.data["status"]["image"] = target.image
    for pod in h.pods.items:
        pod.metadata.namespace = target.namespace
        pod.spec.containers[0].image = target.image
    for item in h.responses["targets"]["activeTargets"]:
        item["labels"].update(namespace=target.namespace, image=target.image)
    for response in h.responses["queries"].values():
        for row in response["result"]:
            if "namespace" in row["metric"]:
                row["metric"].update(namespace=target.namespace, image=target.image)


def test_local_defaults_stay_fixed(targets, harness, execution):
    assert harness.run(execution)["state"] == "applied"
    assert targets.LOCAL_TARGET.namespace == "strawhats-operator-local"
    assert harness.cluster.data["metadata"]["namespace"] == "strawhats-operator-local"


@pytest.mark.parametrize(
    "change",
    [
        {"namespace": "foreign"},
        {"context": "remote"},
        {"name": "other"},
        {"image": "https://foreign/image"},
        {"image_pull_policy": "Always"},
        {"kind": "eks"},
        {"operator_name": "other"},
    ],
)
def test_unbound_targets_are_rejected(rehearsal, change):
    with pytest.raises(ValueError):
        replace(rehearsal, **change)


@pytest.mark.parametrize("replicas", [1, 2])
@pytest.mark.parametrize("rollback", [False, True])
def test_explicit_target_uses_its_namespace_and_image(
    rehearsal, harness, execution, monitoring, replicas, rollback
):
    h = harness
    bind(h, rehearsal)
    if replicas == 1:
        h.pods.items.pop()
        h.cluster.data["spec"]["replicas"] = 1
        h.cluster.data["status"]["readyReplicas"] = 1
        h.responses["targets"]["activeTargets"].pop()
        for response in h.responses["queries"].values():
            response["result"] = [
                r for r in response["result"] if r["metric"].get("instance") != "b"
            ]
    if rollback:
        h.cluster.data["status"]["lastReadySpec"] = deepcopy(h.cluster.data["spec"])
        h.cluster.data["spec"]["mode"] = "Rollback"
        h.cluster.data["status"]["phase"] = "RolledBack"
    with prom_client(h.responses) as prom:
        observation = monitoring.observe(h.cluster, prom, target=rehearsal)
    assert observation.available and observation.target_count == replicas
    assert observation.image == "strawhats-iris:rehearsal-39f81d1"
    result = h.run(execution, target=rehearsal, before_mutation=lambda: None)
    assert result["state"] == "applied"
    assert len(h.writes) == 1
    assert h.writes[0]["spec"]["image"] == "strawhats-iris:rehearsal-39f81d1"


@pytest.mark.parametrize("field", ["namespace", "image", "uid"])
def test_cross_target_observations_never_write(rehearsal, harness, execution, field):
    bind(harness, rehearsal)
    if field == "namespace":
        harness.cluster.data["metadata"][field] = "foreign"
    elif field == "image":
        harness.cluster.data["spec"][field] = "foreign:latest"
    else:
        harness.pods.items[0].metadata.labels["mlops.strawhats.dev/owner-uid"] = (
            "foreign"
        )
    result = harness.run(execution, target=rehearsal, before_mutation=lambda: None)
    assert result["state"] == "unavailable"
    assert harness.calls == 0 and harness.writes == []


def test_nonlocal_apply_without_guard_spends_and_writes_nothing(
    rehearsal, harness, execution
):
    bind(harness, rehearsal)
    result = harness.run(execution, target=rehearsal)
    assert result["state"] == "unavailable"
    assert harness.calls == 0 and harness.writes == []


@pytest.mark.parametrize("seconds", [46, 61])
def test_slow_guard_expires_observation_before_initial_write(
    harness, execution, seconds
):
    def guard():
        harness.clock.seconds += seconds

    result = harness.run(execution, before_mutation=guard)
    assert result["state"] == "unavailable"
    assert harness.calls == 1 and harness.writes == []


def test_mutation_guard_precedes_apply_and_restore(harness, execution):
    harness.outcomes = ["Failed"]
    checks = []

    def guard():
        checks.append(len(harness.writes))
        if harness.writes:
            raise ValueError("Approved window expired")

    result = harness.run(execution, before_mutation=guard)
    assert result["state"] == "unresolved"
    assert checks == [0, 1] and len(harness.writes) == 1


def test_shared_cluster_guards_create_and_rejects_cross_namespace(rehearsal):
    module = importlib.import_module("backend.app.iris_cluster")
    writes, checks = [], []

    def create(*args, **kwargs):
        writes.append((args, kwargs))
        return kwargs["body"]

    cluster = module.IrisCluster(
        None,
        SimpleNamespace(create_namespaced_custom_object=create),
        None,
        target=rehearsal,
        before_mutation=lambda: checks.append(True),
    )
    result = cluster.apply(
        None, {"image": rehearsal.image, "imagePullPolicy": "Never", "replicas": 1}
    )
    assert result["metadata"] == {"namespace": "strawhats", "name": "iris"}
    assert checks == [True] and writes[0][0][2] == "strawhats"
    foreign = {"metadata": {"namespace": "foreign", "name": "iris"}, "spec": {}}
    with pytest.raises((ValueError, HTTPException, MonitoringError)):
        cluster.apply(foreign, {"replicas": 2})
    assert len(writes) == 1
