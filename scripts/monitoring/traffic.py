"""Explicit, bounded real traffic for the one local Iris warning demonstration."""

import argparse
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timezone

import httpx
from fastapi import HTTPException
from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import HTTPError

from backend.app.local_cluster import LocalCluster, connect_local_cluster
from backend.app.local_inference import DeployedPredictionInput, predict_with_service
from scripts.monitoring.observations import (
    PROMETHEUS_URL,
    MonitoringError,
    observe,
    require_snapshot,
    save_evidence,
)

VECTORS = {
    "setosa": [5.1, 3.5, 1.4, 0.2],
    "versicolor": [6.0, 2.9, 4.5, 1.5],
    "virginica": [6.3, 3.3, 6.0, 2.5],
}


def run_demo(
    cluster: LocalCluster,
    prometheus: httpx.Client,
    *,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> dict:
    started = clock()
    report = {
        "complete": False,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "successful_requests": 0,
        "phases": [],
        "observations": [],
    }
    last_request = None

    def guard():
        if clock() - started >= 600:
            raise MonitoringError("The 600-second demo deadline was reached.")

    def snapshot(expected=None):
        guard()
        return require_snapshot(
            cluster,
            expected_generation=expected,
            expected_uid=resource_uid if expected is not None else None,
            before_request=guard,
        )

    def sample():
        snapshot(generation)
        guard()
        result = observe(cluster, prometheus, before_request=guard)
        # An in-flight operation may finish after the deadline; no next work starts.
        snapshot(generation)
        report["observations"].append(
            {"elapsed_seconds": clock() - started, "observation": asdict(result)}
        )
        if not result.available:
            raise MonitoringError("Telemetry became unavailable; this is not recovery.")
        return result

    def predict(label):
        nonlocal last_request
        guard()
        if report["successful_requests"] >= 1000:
            raise MonitoringError("The 1000-request demo budget was reached.")
        if last_request is not None:
            sleep(max(0, last_request + 0.5 - clock()))
        resource = snapshot(generation)
        guard()
        last_request = clock()
        result = predict_with_service(
            cluster,
            resource,
            DeployedPredictionInput(features=VECTORS[label]),
            before_request=guard,
        )
        report["successful_requests"] += 1
        snapshot(generation)
        if result["label"] != label:
            raise MonitoringError(
                "The actual model class did not match the demo input."
            )
        report["last_prediction"] = result

    def healthy_balanced(result):
        return (
            result.alert_state == "inactive"
            and result.recent_predictions is not None
            and result.recent_predictions >= 20
            and set(result.class_shares) == set(VECTORS)
            and max(result.class_shares.values()) <= 0.9
        )

    def phase(name, result):
        report["phases"].append(
            {
                "phase": name,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "elapsed_seconds": clock() - started,
                "observation": asdict(result),
            }
        )
        print(
            f"{name}: {result.alert_state}; {report['successful_requests']} requests",
            flush=True,
        )

    try:
        resource = snapshot()
        generation = resource["metadata"]["generation"]
        resource_uid = resource["metadata"]["uid"]
        report.update(
            generation=generation,
            resource_uid=resource_uid,
            image=resource["status"]["image"],
        )
        sample()  # Refuse to send traffic into an unverified monitoring setup.
        labels = tuple(VECTORS)
        for index in range(60):
            predict(labels[index % 3])
        result = sample()
        index = 60
        for name in ("baseline", "firing", "recovery"):
            next_sample = clock() + 10
            while not (
                healthy_balanced(result)
                if name != "firing"
                else result.alert_state == "firing"
                and result.alert_labels.get("label") == "setosa"
                and result.recent_predictions is not None
                and result.recent_predictions >= 20
                and result.class_shares.get("setosa", 0) > 0.9
            ):
                predict("setosa" if name == "firing" else labels[index % 3])
                index += 1
                if clock() >= next_sample:
                    result = sample()
                    next_sample = clock() + 10
            phase(name, result)
        report.update(complete=True, elapsed_seconds=clock() - started)
        return report
    except (
        MonitoringError,
        HTTPException,
        ApiException,
        HTTPError,
        httpx.HTTPError,
        OSError,
    ) as error:
        report["elapsed_seconds"] = clock() - started
        failure = (
            error
            if isinstance(error, MonitoringError)
            else MonitoringError("A local transport failed; the demo is incomplete.")
        )
        failure.evidence = report
        raise failure from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-demo", required=True, action="store_true")
    parser.parse_args()
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
            report = run_demo(
                cluster, prometheus, clock=time.monotonic, sleep=time.sleep
            )
        path = save_evidence("traffic", report)
        print(f"Verified firing and recovery; evidence: {path}")
        return 0
    except (MonitoringError, HTTPException) as error:
        save_evidence("traffic", getattr(error, "evidence", {"complete": False}))
        print(
            "Local demo stopped; incomplete evidence saved. Check readiness and telemetry."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
