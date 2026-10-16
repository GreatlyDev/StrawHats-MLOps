"""Compute permissible sizing actions from verified per-pod measurements."""

import math
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from backend.app.iris_target import LOCAL_TARGET, RuntimeTarget
from backend.model_operator.spec import Target, choose_target
from scripts.monitoring.observations import Observation


class Action(StrEnum):
    HOLD = "hold"
    INCREASE_CPU = "increase_cpu"
    DECREASE_CPU = "decrease_cpu"
    INCREASE_MEMORY = "increase_memory"
    DECREASE_MEMORY = "decrease_memory"


@dataclass(frozen=True)
class SizingSnapshot:
    resource: dict
    observation: Observation
    collected_at: datetime


@dataclass(frozen=True)
class Candidate:
    action: Action
    target: Target


def _number(value, *, positive=False):
    return (
        type(value) in (int, float)
        and math.isfinite(value)
        and (value > 0 if positive else value >= 0)
    )


def validate_snapshot(
    resource: dict,
    observation: Observation,
    at: datetime,
    *,
    target: RuntimeTarget = LOCAL_TARGET,
) -> SizingSnapshot:
    """Reject insufficient, stale, partial or differently owned observations."""
    try:
        effective = choose_target(resource)
        metadata, status = resource["metadata"], resource["status"]
        pods = observation.pod_usage
        valid = (
            at.utcoffset() is not None
            and metadata["name"] == target.name
            and metadata["namespace"] == target.namespace
            and isinstance(metadata["uid"], str)
            and bool(metadata["uid"])
            and not metadata.get("deletionTimestamp")
            and type(metadata["generation"]) is int
            and metadata["generation"]
            == status["observedGeneration"]
            == observation.generation
            and status["phase"] in ("Ready", "RolledBack")
            and type(status["readyReplicas"]) is int
            and status["readyReplicas"] == effective.replicas
            and effective.image == status["image"] == observation.image == target.image
            and observation.resource_uid == metadata["uid"]
            and observation.available is True
            and observation.alert_state == "inactive"
            and _number(observation.recent_predictions)
            and observation.recent_predictions >= 20
            and type(observation.target_count) is int
            and len(pods) == observation.target_count == effective.replicas
            and len({pod.name for pod in pods})
            == len({pod.uid for pod in pods})
            == len(pods)
            and set(observation.target_pods) == {pod.name for pod in pods}
            and all(
                isinstance(pod.name, str)
                and bool(pod.name)
                and isinstance(pod.uid, str)
                and bool(pod.uid)
                and _number(pod.cpu_cores)
                and _number(pod.resident_bytes, positive=True)
                and _number(pod.sample_time)
                and -5 <= at.timestamp() - pod.sample_time <= 45
                for pod in pods
            )
        )
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        valid = False
    if not valid:
        raise ValueError(
            "Sizing requires a current ready Iris revision and complete fresh telemetry with at least 20 recent predictions and no distribution alert."
        )
    return SizingSnapshot(deepcopy(resource), deepcopy(observation), at)


def candidates(snapshot: SizingSnapshot) -> tuple[Candidate, ...]:
    target = choose_target(snapshot.resource)
    cpu = max(pod.cpu_cores for pod in snapshot.observation.pod_usage) * 1000
    memory = max(pod.resident_bytes for pod in snapshot.observation.pod_usage) / 2**20
    pressure = cpu / target.cpu_millicores >= 0.75 or memory / target.memory_mi >= 0.80
    result = [Candidate(Action.HOLD, target)]
    for (
        field,
        current,
        measured,
        ladder,
        increase_at,
        decrease_at,
        headroom,
        up,
        down,
    ) in (
        (
            "cpu_millicores",
            target.cpu_millicores,
            cpu,
            (100, 250, 500),
            0.75,
            0.20,
            0.50,
            Action.INCREASE_CPU,
            Action.DECREASE_CPU,
        ),
        (
            "memory_mi",
            target.memory_mi,
            memory,
            (128, 256, 512),
            0.80,
            0.50,
            0.60,
            Action.INCREASE_MEMORY,
            Action.DECREASE_MEMORY,
        ),
    ):
        higher = next((value for value in ladder if value > current), None)
        lower = next((value for value in reversed(ladder) if value < current), None)
        if higher is not None and measured / current >= increase_at:
            result.append(Candidate(up, replace(target, **{field: higher})))
        if (
            lower is not None
            and not pressure
            and measured / current <= decrease_at
            and measured / lower <= headroom
        ):
            result.append(Candidate(down, replace(target, **{field: lower})))
    return tuple(result)
