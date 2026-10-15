"""Guard sizing decisions against real threshold, ownership and freshness errors."""

import importlib
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from scripts.monitoring import observations as monitoring

AT = datetime(2026, 10, 15, 17, tzinfo=timezone.utc)


@pytest.fixture
def policy():
    try:
        return importlib.import_module("scripts.optimization.policy")
    except ModuleNotFoundError:
        pytest.fail("Verified sizing policy is not implemented")


@pytest.fixture
def sizing_data(policy):
    resource = {
        "metadata": {
            "name": "iris",
            "namespace": "strawhats-operator-local",
            "uid": "iris-owner",
            "generation": 8,
            "resourceVersion": "21",
        },
        "spec": {
            "mode": "Deploy",
            "image": monitoring.IMAGE,
            "imagePullPolicy": "Never",
            "replicas": 2,
            "cpuMillicores": 250,
            "memoryMi": 256,
        },
        "status": {
            "phase": "Ready",
            "observedGeneration": 8,
            "readyReplicas": 2,
            "image": monitoring.IMAGE,
        },
    }
    observation = monitoring.Observation(
        generation=8,
        image=monitoring.IMAGE,
        target_count=2,
        available=True,
        recent_predictions=20,
        class_shares={"setosa": 0.4, "versicolor": 0.3, "virginica": 0.3},
        alert_state="inactive",
        process_cpu_cores=0.02,
        resident_memory_bytes=120 * 2**20,
        target_pods=("iris-a", "iris-b"),
        resource_uid="iris-owner",
        pod_usage=tuple(
            monitoring.PodUsage(name, uid, 0.01, 60 * 2**20, AT.timestamp())
            for name, uid in (("iris-a", "pod-a"), ("iris-b", "pod-b"))
        ),
    )
    return resource, observation


def choices(policy, data):
    return {
        item.action.value: item.target.to_spec()
        for item in policy.candidates(policy.validate_snapshot(*data, AT))
    }


def test_low_measured_usage_offers_single_field_changes(policy, sizing_data):
    result = choices(policy, sizing_data)
    assert set(result) == {"hold", "decrease_cpu", "decrease_memory"}
    assert result["decrease_cpu"] == {
        "image": monitoring.IMAGE,
        "imagePullPolicy": "Never",
        "replicas": 2,
        "cpuMillicores": 100,
        "memoryMi": 256,
    }
    assert result["decrease_memory"]["cpuMillicores"] == 250
    assert result["decrease_memory"]["memoryMi"] == 128


def test_busiest_pod_prevents_unsafe_average_based_downsize(policy, sizing_data):
    resource, observation = sizing_data
    usage = (
        replace(observation.pod_usage[0], cpu_cores=0.19),
        observation.pod_usage[1],
    )
    result = choices(policy, (resource, replace(observation, pod_usage=usage)))
    assert set(result) == {"hold", "increase_cpu"}
    assert result["increase_cpu"]["cpuMillicores"] == 500


@pytest.mark.parametrize(
    "cpu,want",
    [
        (0.1875, "increase_cpu"),
        (0.18749, None),
        (0.05, "decrease_cpu"),
        (0.05001, None),
    ],
)
def test_cpu_thresholds_and_smaller_request_headroom(policy, sizing_data, cpu, want):
    resource, observation = sizing_data
    usage = tuple(
        replace(p, cpu_cores=cpu, resident_bytes=130 * 2**20)
        for p in observation.pod_usage
    )
    result = choices(policy, (resource, replace(observation, pod_usage=usage)))
    assert set(result) == ({"hold", want} if want else {"hold"})


@pytest.mark.parametrize(
    "memory,want",
    [
        (204.8, "increase_memory"),
        (204.799, None),
        (76.8, "decrease_memory"),
        (76.801, None),
        (128, None),
    ],
)
def test_memory_thresholds_and_smaller_request_headroom(
    policy, sizing_data, memory, want
):
    resource, observation = sizing_data
    usage = tuple(
        replace(p, cpu_cores=0.06, resident_bytes=memory * 2**20)
        for p in observation.pod_usage
    )
    result = choices(policy, (resource, replace(observation, pod_usage=usage)))
    assert set(result) == ({"hold", want} if want else {"hold"})


def test_memory_pressure_excludes_cpu_reduction(policy, sizing_data):
    resource, observation = sizing_data
    usage = tuple(replace(p, resident_bytes=220 * 2**20) for p in observation.pod_usage)
    assert set(choices(policy, (resource, replace(observation, pod_usage=usage)))) == {
        "hold",
        "increase_memory",
    }


@pytest.mark.parametrize("cpu,memory", [(100, 128), (500, 512), (200, 192)])
def test_ladder_endpoints_and_manual_intermediate_requests(
    policy, sizing_data, cpu, memory
):
    resource, observation = sizing_data
    resource["spec"].update(cpuMillicores=cpu, memoryMi=memory)
    result = choices(policy, (resource, observation))
    if cpu == 100:
        assert set(result) == {"hold"}
    else:
        assert result["decrease_cpu"]["cpuMillicores"] == (250 if cpu == 500 else 100)
        assert result["decrease_memory"]["memoryMi"] == (256 if memory == 512 else 128)


def test_rollback_uses_verified_effective_resources(policy, sizing_data):
    resource, observation = sizing_data
    resource["status"]["lastReadySpec"] = {**resource["spec"], "cpuMillicores": 500}
    resource["spec"].update(mode="Rollback", cpuMillicores=100)
    assert (
        choices(policy, (resource, observation))["decrease_cpu"]["cpuMillicores"] == 250
    )


@pytest.mark.parametrize(
    "age,accepted", [(45, True), (45.01, False), (-5, True), (-5.01, False)]
)
def test_query_sample_age_boundary(policy, sizing_data, age, accepted):
    resource, observation = sizing_data
    usage = tuple(
        replace(p, sample_time=(AT - timedelta(seconds=age)).timestamp())
        for p in observation.pod_usage
    )
    data = resource, replace(observation, pod_usage=usage)
    if accepted:
        assert "decrease_cpu" in choices(policy, data)
    else:
        with pytest.raises(ValueError):
            policy.validate_snapshot(*data, AT)


@pytest.mark.parametrize(
    "field,bad",
    [
        ("cpu_cores", True),
        ("cpu_cores", -0.1),
        ("cpu_cores", float("nan")),
        ("cpu_cores", float("inf")),
        ("resident_bytes", False),
        ("resident_bytes", 0),
        ("sample_time", "now"),
        ("sample_time", float("nan")),
    ],
)
def test_invalid_measurement_never_produces_a_candidate(
    policy, sizing_data, field, bad
):
    resource, observation = sizing_data
    usage = (
        replace(observation.pod_usage[0], **{field: bad}),
        observation.pod_usage[1],
    )
    with pytest.raises(ValueError):
        policy.validate_snapshot(resource, replace(observation, pod_usage=usage), AT)


@pytest.mark.parametrize(
    "fault",
    [
        "unavailable",
        "pending",
        "firing",
        "uid",
        "generation",
        "unobserved",
        "foreign",
        "missing",
        "duplicate",
        "missing_uid",
        "deleting",
        "few_predictions",
        "boolean_predictions",
        "wrong_image",
    ],
)
def test_incomplete_or_changed_workload_cannot_be_sized(policy, sizing_data, fault):
    resource, observation = deepcopy(sizing_data)
    if fault in ("pending", "firing"):
        observation = replace(observation, alert_state=fault)
    elif fault == "unavailable":
        observation = replace(observation, available=False)
    elif fault == "uid":
        observation = replace(observation, resource_uid="replacement")
    elif fault == "generation":
        resource["metadata"]["generation"] = 9
    elif fault == "unobserved":
        resource["status"]["observedGeneration"] = 7
    elif fault == "foreign":
        observation = replace(observation, target_pods=("iris-a", "other"))
    elif fault == "missing":
        observation = replace(observation, pod_usage=observation.pod_usage[:1])
    elif fault == "duplicate":
        observation = replace(observation, pod_usage=(observation.pod_usage[0],) * 2)
    elif fault == "missing_uid":
        observation = replace(
            observation,
            pod_usage=(
                replace(observation.pod_usage[0], uid=""),
                observation.pod_usage[1],
            ),
        )
    elif fault == "deleting":
        resource["metadata"]["deletionTimestamp"] = AT.isoformat()
    elif fault in ("few_predictions", "boolean_predictions"):
        observation = replace(
            observation, recent_predictions=19 if fault == "few_predictions" else True
        )
    else:
        resource["spec"]["image"] = "other:latest"
    with pytest.raises(ValueError):
        policy.validate_snapshot(resource, observation, AT)


def test_snapshot_defensively_copies_source_data(policy, sizing_data):
    resource, observation = sizing_data
    snapshot = policy.validate_snapshot(resource, observation, AT)
    resource["spec"]["replicas"] = 1
    observation.class_shares.clear()
    assert snapshot.resource["spec"]["replicas"] == 2
    assert snapshot.observation.class_shares == {
        "setosa": 0.4,
        "versicolor": 0.3,
        "virginica": 0.3,
    }
