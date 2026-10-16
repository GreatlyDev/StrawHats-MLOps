"""Read the fixed local Iris workload and its actual Prometheus observations."""

import argparse
import json
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Literal

import httpx
from fastapi import HTTPException
from kubernetes import client

from backend.app.iris_target import LOCAL_TARGET, RuntimeTarget
from backend.app.local_cluster import (
    TIMEOUT,
    LocalCluster,
    connect_local_cluster,
)
from backend.model_operator.spec import choose_target

IMAGE, NAME, NAMESPACE = LOCAL_TARGET.image, LOCAL_TARGET.name, LOCAL_TARGET.namespace

PROMETHEUS_URL = "http://127.0.0.1:9090"
ALERT_NAME = "IrisPredictionDistributionSkew"
LABELS = {"setosa", "versicolor", "virginica"}
CLASS_COUNT = 'count by(instance)(strawhats_model_prediction_class_total{job="iris-model",label=~"setosa|versicolor|virginica"})'
CPU = 'rate(process_cpu_seconds_total{job="iris-model"}[2m])'
MEMORY = 'process_resident_memory_bytes{job="iris-model"}'
RAW_CPU = 'process_cpu_seconds_total{job="iris-model"}'
RAW_CLASSES = 'strawhats_model_prediction_class_total{job="iris-model",label=~"setosa|versicolor|virginica"}'
RECORDINGS = (
    "iris:target_count",
    "iris:telemetry_available",
    "iris:class_predictions:increase2m",
    "iris:predictions:increase2m",
    "iris:class_share:ratio2m",
)
EVIDENCE = Path(__file__).resolve().parents[2] / ".local" / "milestone-four"


class MonitoringError(RuntimeError):
    """A local verification failed without making an infrastructure change."""


@dataclass(frozen=True)
class PodUsage:
    name: str
    uid: str
    cpu_cores: float
    resident_bytes: float
    sample_time: float


@dataclass(frozen=True)
class Observation:
    generation: int
    image: str
    target_count: int
    available: bool
    recent_predictions: float | None
    class_shares: dict[str, float]
    alert_state: Literal["inactive", "pending", "firing", "unavailable"]
    process_cpu_cores: float | None
    resident_memory_bytes: float | None
    target_pods: tuple[str, ...] = ()
    alert_labels: dict[str, str] = field(default_factory=dict)
    resource_uid: str = ""
    pod_usage: tuple[PodUsage, ...] = ()


def require_snapshot(
    cluster: LocalCluster,
    expected_generation: int | None = None,
    *,
    expected_uid: str | None = None,
    before_request: Callable[[], None] | None = None,
    request_timeout=None,
    target: RuntimeTarget = LOCAL_TARGET,
) -> dict:
    if before_request:
        before_request()

    def options():
        return (
            {}
            if request_timeout is None
            else {
                "request_timeout": request_timeout()
                if callable(request_timeout)
                else request_timeout
            }
        )

    resource = cluster.resource(**options())
    try:
        effective = choose_target(resource)
        metadata, status = resource["metadata"], resource["status"]
        generation = metadata["generation"]
        valid = (
            metadata["name"] == target.name
            and metadata["namespace"] == target.namespace
            and isinstance(metadata.get("uid"), str)
            and bool(metadata["uid"])
            and (expected_uid is None or metadata["uid"] == expected_uid)
            and not metadata.get("deletionTimestamp")
            and type(generation) is int
            and status["observedGeneration"] == generation
            and status["phase"] in ("Ready", "RolledBack")
            and type(status["readyReplicas"]) is int
            and status["readyReplicas"] == effective.replicas
            and effective.image == status["image"] == target.image
            and (expected_generation is None or generation == expected_generation)
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if valid:
        if before_request:
            before_request()
        valid = cluster.controller_ready(**options())
    if not valid:
        raise MonitoringError(
            "Local Iris must have a current ready controller, observed revision and monitoring image. Stop if the deployment changes."
        )
    return resource


def _fetch(
    prometheus: httpx.Client, path: str, *, before_request=None, **params
) -> dict:
    if str(prometheus.base_url).rstrip("/") != PROMETHEUS_URL:
        raise MonitoringError(
            "Prometheus must use the fixed loopback address on port 9090."
        )
    try:
        if before_request:
            before_request()
        response = prometheus.get(path, params=params, follow_redirects=False)
        response.raise_for_status()
        payload = response.json()
        if payload["status"] != "success" or not isinstance(payload["data"], dict):
            raise ValueError
        return payload["data"]
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        raise MonitoringError(
            "Local Prometheus returned unavailable or malformed observations."
        ) from None


def _rows(data, key, *, labels_key=None):
    rows = data.get(key)
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise MonitoringError("Local Prometheus returned a malformed collection.")
    if labels_key and not all(
        isinstance(row.get(labels_key), dict)
        and all(isinstance(value, str) for value in row[labels_key].values())
        for row in rows
    ):
        raise MonitoringError("Local Prometheus returned malformed labels.")
    return rows


def _query(prometheus, expression, *, before_request=None):
    result = _fetch(
        prometheus, "/api/v1/query", before_request=before_request, query=expression
    )
    if result.get("resultType") != "vector" or not isinstance(
        result.get("result"), list
    ):
        raise MonitoringError("Local Prometheus returned an invalid vector.")
    return _rows(result, "result", labels_key="metric")


def _number(row):
    try:
        value = row["value"][1]
        if not isinstance(value, str):
            return None
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _scalar(rows):
    return _number(rows[0]) if len(rows) == 1 else None


def _instances(rows):
    result = {}
    for row in rows:
        instance = row.get("metric", {}).get("instance")
        number = _number(row)
        if not instance or instance in result or number is None:
            return {}
        result[instance] = number
    return result


def _fresh(target):
    return _fresh_time(target.get("lastScrape"))


def _fresh_time(value):
    try:
        if not isinstance(value, str):
            return False
        stamped = datetime.fromisoformat(value.replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - stamped).total_seconds()
        return -5 <= age <= 45
    except (KeyError, TypeError, ValueError):
        return False


def _pod_usage(pods, targets, cpu_rows, memory_rows, source_times):
    """Keep per-instance measurements attributed to the observed pod identity."""
    try:
        cpus = {row["metric"]["instance"]: row for row in cpu_rows}
        memories = {row["metric"]["instance"]: row for row in memory_rows}
        if len(cpus) != len(cpu_rows) or len(memories) != len(memory_rows):
            return ()
        identities = {pod["metadata"]["name"]: pod["metadata"]["uid"] for pod in pods}
        result = []
        for target in targets:
            labels = target["labels"]
            cpu, memory = cpus[labels["instance"]], memories[labels["instance"]]
            times = (
                cpu["value"][0],
                memory["value"][0],
                source_times[labels["instance"]],
            )
            if not all(
                type(value) in (int, float) and math.isfinite(value) for value in times
            ):
                return ()
            if _number(cpu) is None or _number(memory) is None:
                return ()
            result.append(
                PodUsage(
                    labels["pod"],
                    identities[labels["pod"]],
                    _number(cpu),
                    _number(memory),
                    min(times),
                )
            )
        return tuple(sorted(result, key=lambda pod: pod.name))
    except (KeyError, IndexError, TypeError):
        return ()


def _fresh_sample(row):
    try:
        timestamp = row["value"][0]
        return (
            type(timestamp) in (int, float)
            and math.isfinite(timestamp)
            and -5 <= datetime.now(timezone.utc).timestamp() - timestamp <= 45
        )
    except (KeyError, IndexError, TypeError):
        return False


def _source_times(query, targets):
    """timestamp() returns source age in value[1]; value[0] is evaluation time."""
    expected = {target["labels"]["instance"]: target["labels"] for target in targets}
    values = {}
    for selector in (RAW_CPU, MEMORY, RAW_CLASSES, *RECORDINGS):
        rows = query(f"timestamp({selector})")
        raw = selector in (RAW_CPU, MEMORY, RAW_CLASSES)
        labelled = selector in (
            RAW_CLASSES,
            "iris:class_predictions:increase2m",
            "iris:class_share:ratio2m",
        )
        wanted = (
            {(instance, label) for instance in expected for label in LABELS}
            if raw and labelled
            else set(expected)
            if raw
            else LABELS
            if labelled
            else {"scalar"}
        )
        samples = {}
        for row in rows:
            labels = row["metric"]
            instance = labels.get("instance")
            identity = (
                (instance, labels.get("label"))
                if raw and labelled
                else instance
                if raw
                else labels.get("label")
                if labelled
                else "scalar"
            )
            timestamp = _number(row)
            if (
                identity not in wanted
                or identity in samples
                or timestamp is None
                or not -5 <= datetime.now(timezone.utc).timestamp() - timestamp <= 45
                or (
                    raw
                    and any(
                        labels.get(key) != expected[instance].get(key)
                        for key in ("job", "namespace", "pod", "image")
                    )
                )
            ):
                return {}
            samples[identity] = timestamp
        if set(samples) != wanted:
            return {}
        values[selector] = samples
    oldest_rule = min(
        stamped for selector in RECORDINGS for stamped in values[selector].values()
    )
    return {
        instance: min(
            values[RAW_CPU][instance],
            values[MEMORY][instance],
            oldest_rule,
            *(values[RAW_CLASSES][instance, label] for label in LABELS),
        )
        for instance in expected
    }


def _sizing_rules_valid(rules):
    for name in (*RECORDINGS, ALERT_NAME):
        matches = [rule for rule in rules if rule.get("name") == name]
        if len(matches) != 1:
            return False
        rule = matches[0]
        if (
            rule.get("type") != ("alerting" if name == ALERT_NAME else "recording")
            or rule.get("health") != "ok"
            or rule.get("lastError")
            or not _fresh_time(rule.get("lastEvaluation"))
        ):
            return False
    return True


def observe(
    cluster: LocalCluster,
    prometheus: httpx.Client,
    *,
    before_request: Callable[[], None] | None = None,
    target: RuntimeTarget = LOCAL_TARGET,
) -> Observation:
    resource = require_snapshot(cluster, before_request=before_request, target=target)
    fetch = partial(_fetch, prometheus, before_request=before_request)
    query_samples = []

    def query(expression):
        rows = _query(prometheus, expression, before_request=before_request)
        query_samples.extend(rows)
        return rows

    if before_request:
        before_request()
    pods = cluster.sdk.sanitize_for_serialization(
        client.CoreV1Api(cluster.sdk).list_namespaced_pod(
            target.namespace,
            label_selector=f"mlops.strawhats.dev/owner-uid={resource['metadata']['uid']}",
            _request_timeout=TIMEOUT,
        )
    )["items"]
    names = {pod["metadata"]["name"] for pod in pods}
    pod_valid = len(names) == resource["status"]["readyReplicas"] and all(
        pod["metadata"].get("namespace") == target.namespace
        and not pod["metadata"].get("deletionTimestamp")
        and pod["metadata"].get("labels", {}).get("mlops.strawhats.dev/owner-uid")
        == resource["metadata"]["uid"]
        and any(
            container["name"] == "iris" and container["image"] == target.image
            for container in pod["spec"]["containers"]
        )
        and any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in pod.get("status", {}).get("conditions", [])
        )
        for pod in pods
    )
    targets = [
        item
        for item in _rows(
            fetch("/api/v1/targets"), "activeTargets", labels_key="labels"
        )
        if item.get("labels", {}).get("job") == "iris-model"
    ]
    instances = {item.get("labels", {}).get("instance") for item in targets}
    target_valid = (
        len(targets) == len(names) == len(instances)
        and None not in instances
        and {item.get("labels", {}).get("pod") for item in targets} == names
        and all(
            item.get("labels", {}).get("namespace") == target.namespace
            and item.get("labels", {}).get("image") == target.image
            and item.get("health") == "up"
            and not item.get("lastError")
            and _fresh(item)
            for item in targets
        )
    )
    all_rules = [
        rule
        for group in _rows(fetch("/api/v1/rules"), "groups")
        for rule in _rows(group, "rules")
    ]
    rules = [rule for rule in all_rules if rule.get("name") == ALERT_NAME]
    rule_valid = (
        len(rules) == 1
        and rules[0].get("type") == "alerting"
        and rules[0].get("health") == "ok"
        and not rules[0].get("lastError")
    )
    alerts = [
        item
        for item in _rows(fetch("/api/v1/alerts"), "alerts", labels_key="labels")
        if item.get("labels", {}).get("alertname") == ALERT_NAME
    ]
    counts = _instances(query(CLASS_COUNT))
    cpu_rows, memory_rows = query(CPU), query(MEMORY)
    cpu, memory = _instances(cpu_rows), _instances(memory_rows)
    flag = _scalar(query("iris:telemetry_available"))
    recent = _scalar(query("iris:predictions:increase2m"))
    shares = {}
    for row in query("iris:class_share:ratio2m"):
        label, value = row.get("metric", {}).get("label"), _number(row)
        if label not in LABELS or label in shares or value is None or value > 1:
            shares = {}
            break
        shares[label] = value
    shares_valid = recent is not None and (
        recent == 0
        or (
            set(shares) == LABELS
            and math.isclose(sum(shares.values()), 1, abs_tol=1e-6)
        )
    )
    alert_valid = len(alerts) <= 1 and all(
        item.get("state") in ("pending", "firing")
        and item.get("labels", {}).get("label") in LABELS
        and item.get("labels", {}).get("severity") == "warning"
        for item in alerts
    )
    available = bool(
        pod_valid
        and target_valid
        and rule_valid
        and alert_valid
        and flag == 1
        and instances
        and set(counts) == set(cpu) == set(memory) == instances
        and all(number == 3 for number in counts.values())
        and all(number > 0 for number in memory.values())
        and shares_valid
    )
    if not available:
        shares = {}
    state = (
        (alerts[0]["state"] if alerts else "inactive") if available else "unavailable"
    )
    labels = (
        {key: alerts[0]["labels"][key] for key in ("alertname", "label", "severity")}
        if available and alerts
        else {}
    )
    source_times = {}
    if available and _sizing_rules_valid(all_rules):
        try:
            source_times = _source_times(query, targets)
        except MonitoringError:
            pass  # Missing freshness evidence prevents sizing, preserving dashboard reads.
    return Observation(
        resource["metadata"]["generation"],
        target.image,
        len(targets),
        available,
        recent,
        shares,
        state,
        sum(cpu.values()) if available else None,
        sum(memory.values()) if available else None,
        tuple(sorted(names)),
        labels,
        resource["metadata"]["uid"],
        _pod_usage(pods, targets, cpu_rows, memory_rows, source_times)
        if available
        and source_times
        and all(_fresh_sample(row) for row in query_samples)
        else (),
    )


def save_evidence(name: str, payload: dict) -> Path:
    if name not in ("observation", "traffic"):
        raise MonitoringError("Unknown evidence kind.")
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE / f"{name}.json"
    path.write_text(
        json.dumps(
            {"recorded_at": datetime.now(timezone.utc).isoformat(), **payload},
            indent=2,
            allow_nan=False,
        ),
        encoding="utf-8",
    )
    return path


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    try:
        with (
            connect_local_cluster() as cluster,
            httpx.Client(
                base_url=PROMETHEUS_URL,
                trust_env=False,
                follow_redirects=False,
                timeout=5,
            ) as prometheus,
        ):
            result = observe(cluster, prometheus)
        path = save_evidence("observation", asdict(result))
        print(
            json.dumps(
                {
                    "available": result.available,
                    "targets": result.target_count,
                    "alert_state": result.alert_state,
                    "evidence": str(path),
                }
            )
        )
        return 0 if result.available else 1
    except (MonitoringError, HTTPException) as error:
        print(str(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
