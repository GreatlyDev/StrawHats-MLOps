"""Apply at most one verified local Iris sizing change with guarded restoration."""

import json
import os
import tempfile
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi import HTTPException
from kubernetes import client
from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import HTTPError

from backend.app.iris_target import LOCAL_TARGET, RuntimeTarget
from backend.app.local_cluster import GROUP, PLURAL, VERSION
from backend.app.local_inference import DeployedPredictionInput, predict_with_service
from backend.model_operator.reconcile import _matches
from backend.model_operator.spec import choose_target
from backend.model_operator.workload import render_workload
from scripts.monitoring.observations import MonitoringError, observe, require_snapshot

from .advisor import Decision, RecommendationUnavailable, recommend
from .policy import Action, candidates, validate_snapshot

STAMP = "mlops.strawhats.dev/last-adjustment-at"
RUN = "mlops.strawhats.dev/last-adjustment-id"
CONNECTION_ERRORS = (HTTPError, OSError, httpx.HTTPError)


class Superseded(RuntimeError):
    """A different revision owns the workload; never restore over it."""


class RolloutFailed(RuntimeError):
    """A known accepted revision failed readiness or its bounded deadline."""


def save_evidence(path: Path, payload: dict) -> None:
    """Persist the captured prior settings before any write, then replace atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as file:
            temporary = Path(file.name)
            json.dump(payload, file, indent=2, allow_nan=False)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def _cooldown(resource, at):
    annotations = resource["metadata"].get("annotations") or {}
    if STAMP not in annotations and RUN not in annotations:
        return False
    try:
        stamped = datetime.fromisoformat(annotations[STAMP].replace("Z", "+00:00"))
        age = (at - stamped).total_seconds()
        if (
            stamped.utcoffset() is None
            or age < 0
            or not isinstance(annotations[RUN], str)
            or not annotations[RUN].strip()
        ):
            raise ValueError
        return age < 300
    except (KeyError, AttributeError, TypeError, ValueError, OverflowError):
        raise ValueError(
            "Cooldown annotations are malformed or future-dated."
        ) from None


def _identity(current, accepted):
    try:
        return (
            current["metadata"]["uid"] == accepted["metadata"]["uid"]
            and current["metadata"]["generation"] == accepted["metadata"]["generation"]
            and not current["metadata"].get("deletionTimestamp")
            and current["spec"] == accepted["spec"]
            and all(
                current["metadata"].get("annotations", {}).get(key)
                == accepted["metadata"].get("annotations", {}).get(key)
                for key in (STAMP, RUN)
            )
        )
    except (KeyError, TypeError):
        return False


def _patch(cluster, source, target, annotations, *, runtime):
    metadata = source["metadata"]
    if not metadata.get("resourceVersion"):
        raise ValueError("A current resourceVersion is required.")
    return cluster.custom.patch_namespaced_custom_object(
        GROUP,
        VERSION,
        runtime.namespace,
        PLURAL,
        runtime.name,
        body={
            "metadata": {
                "uid": metadata["uid"],
                "resourceVersion": metadata["resourceVersion"],
                "annotations": annotations,
            },
            "spec": {**target.to_spec(), "mode": "Deploy"},
        },
        _request_timeout=5,
    )


def _expected(source, target, annotations):
    result = deepcopy(source)
    result["metadata"]["generation"] += 1
    result["metadata"].setdefault("annotations", {}).update(annotations)
    result["spec"].update(target.to_spec(), mode="Deploy")
    return result


def verify_revision(cluster, accepted, *, monotonic, wait, runtime):
    deadline = monotonic() + 180

    def timeout():
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise RolloutFailed("Readiness deadline reached.")
        return min(5.0, remaining)

    def same(resource):
        timeout()
        if not _identity(resource, accepted):
            raise Superseded(
                "Iris changed during verification; no restoration attempted."
            )

    while True:
        resource = cluster.resource(request_timeout=timeout())
        same(resource)
        status = resource.get("status", {})
        if status.get("observedGeneration") == resource["metadata"][
            "generation"
        ] and status.get("phase") in ("Failed", "Conflict"):
            raise RolloutFailed("The operator reported rollout failure.")
        try:
            resource = require_snapshot(
                cluster,
                accepted["metadata"]["generation"],
                expected_uid=accepted["metadata"]["uid"],
                before_request=timeout,
                request_timeout=timeout,
                target=runtime,
            )
            same(resource)
            target = choose_target(resource)
            pods = cluster.sdk.sanitize_for_serialization(
                client.CoreV1Api(cluster.sdk).list_namespaced_pod(
                    runtime.namespace,
                    label_selector=f"mlops.strawhats.dev/owner-uid={resource['metadata']['uid']}",
                    _request_timeout=timeout(),
                )
            )["items"]
            timeout()
            desired = render_workload(resource, target)[0]["spec"]["template"]["spec"][
                "containers"
            ][0]["resources"]
            ready = {
                pod["metadata"]["uid"]
                for pod in pods
                if pod["metadata"].get("namespace") == runtime.namespace
                and not pod["metadata"].get("deletionTimestamp")
                and pod["metadata"]
                .get("labels", {})
                .get("mlops.strawhats.dev/owner-uid")
                == resource["metadata"]["uid"]
                and pod.get("status", {}).get("phase") == "Running"
                and any(
                    c.get("type") == "Ready" and c.get("status") == "True"
                    for c in pod.get("status", {}).get("conditions", [])
                )
                and any(
                    c.get("name") == "iris"
                    and c.get("image") == runtime.image
                    and _matches(c.get("resources"), desired)
                    for c in pod["spec"]["containers"]
                )
            }
            slices = cluster.sdk.sanitize_for_serialization(
                client.DiscoveryV1Api(cluster.sdk).list_namespaced_endpoint_slice(
                    runtime.namespace,
                    label_selector="kubernetes.io/service-name=iris",
                    _request_timeout=timeout(),
                )
            )["items"]
            timeout()
            endpoints = {
                ep["targetRef"]["uid"]
                for item in slices
                if item.get("metadata", {}).get("namespace") == runtime.namespace
                and item.get("metadata", {})
                .get("labels", {})
                .get("kubernetes.io/service-name")
                == runtime.name
                and not item.get("metadata", {}).get("deletionTimestamp")
                and any(
                    p.get("name") == "http"
                    and p.get("port") == 8000
                    and p.get("protocol", "TCP") == "TCP"
                    for p in item.get("ports", [])
                )
                for ep in item.get("endpoints", [])
                if ep.get("conditions", {}).get("ready") is True
                and not ep.get("conditions", {}).get("terminating", False)
                and ep.get("targetRef", {}).get("kind") == "Pod"
                and ep.get("targetRef", {}).get("namespace") == runtime.namespace
            }
            if len(ready) == target.replicas and ready == endpoints:
                prediction = predict_with_service(
                    cluster,
                    resource,
                    DeployedPredictionInput(features=[5.1, 3.5, 1.4, 0.2]),
                    before_request=timeout,
                    request_timeout=timeout,
                    target=runtime,
                )
                same(cluster.resource(request_timeout=timeout()))
                return {
                    "generation": resource["metadata"]["generation"],
                    "ready_replicas": len(ready),
                    "prediction": prediction,
                }
        except MonitoringError:
            pass
        except HTTPException as error:
            if error.status_code in (409, 502):
                raise RolloutFailed(
                    "The owned Service did not verify valid Iris inference."
                ) from None
            raise
        wait(min(2.0, timeout()))


def run_adjustment(
    cluster,
    prometheus,
    *,
    apply: bool,
    key: str | None,
    model: str,
    evidence_dir: Path,
    advisor=recommend,
    now=lambda: datetime.now(timezone.utc),
    monotonic=time.monotonic,
    wait=time.sleep,
    target: RuntimeTarget = LOCAL_TARGET,
    before_mutation=None,
) -> dict:
    run_id = str(uuid4())
    path = evidence_dir / f"{now().strftime('%Y%m%dT%H%M%S%fZ')}-{run_id}.json"
    result = {
        "state": "unavailable",
        "action": "hold",
        "run_id": run_id,
        "evidence": str(path),
    }

    def finish(state, message):
        result.update(state=state, message=message)
        try:
            save_evidence(path, result)
        except OSError:
            result["evidence_warning"] = (
                "Evidence could not be saved; inspect the live resource before another run."
            )
        return result

    try:
        if apply and target.kind != "local" and before_mutation is None:
            return finish(
                "unavailable", "A verified runtime mutation guard is required."
            )
        resource = cluster.resource()
        if not resource:
            return finish(
                "unavailable", "The existing local Iris resource is unavailable."
            )
        if _cooldown(resource, now()):
            return finish(
                "held", "The five-minute automatic-change cooldown is active."
            )
        snapshot = validate_snapshot(
            resource, observe(cluster, prometheus, target=target), now(), target=target
        )
        prior = choose_target(snapshot.resource)
        result["before"] = prior.to_spec()
        choices = candidates(snapshot)
        if len(choices) == 1:
            return finish("held", "No bounded resource change is eligible.")
        if not key or not key.strip():
            return finish(
                "unavailable", "OpenAI server credentials are not configured."
            )
        started = monotonic()
        decision = Decision.model_validate(
            advisor(snapshot, choices, key=key, model=model)
        )
        resource = cluster.resource()
        fresh = validate_snapshot(
            resource, observe(cluster, prometheus, target=target), now(), target=target
        )
        if (
            monotonic() - started > 60
            or resource["metadata"]["uid"] != snapshot.resource["metadata"]["uid"]
            or resource["metadata"]["generation"]
            != snapshot.resource["metadata"]["generation"]
            or choose_target(resource) != prior
            or {p.uid for p in fresh.observation.pod_usage}
            != {p.uid for p in snapshot.observation.pod_usage}
            or _cooldown(resource, now())
        ):
            return finish(
                "unavailable",
                "The decision expired or the observed Iris revision changed.",
            )
        offered = {c.action: c.target for c in choices}
        eligible = {c.action: c.target for c in candidates(fresh)}
        if (
            decision.action not in offered
            or eligible.get(decision.action) != offered[decision.action]
        ):
            return finish("unavailable", "The selected action is no longer eligible.")
        result["action"] = decision.action.value
        proposed = offered[decision.action]
        result["after"] = proposed.to_spec()
        if decision.action == Action.HOLD:
            return finish("held", "The AI selected Hold; no change was made.")
        if not apply:
            return finish("preview", "Preview only; no cluster write was made.")
        if not resource["metadata"].get("resourceVersion"):
            raise ValueError("A current resourceVersion is required.")
        annotations = {STAMP: now().astimezone(timezone.utc).isoformat(), RUN: run_id}
        expected = _expected(resource, proposed, annotations)
        result["state"] = "pending"
        save_evidence(path, result)
    except RecommendationUnavailable as error:
        # The advisor translates SDK failures to static, sanitized project messages.
        return finish("unavailable", str(error))
    except (
        ValueError,
        KeyError,
        TypeError,
        MonitoringError,
        HTTPException,
        ApiException,
        *CONNECTION_ERRORS,
    ):
        return finish(
            "unavailable",
            "Fresh local observations, credentials and a valid permitted AI decision are required; no change was made.",
        )

    try:
        if before_mutation:
            before_mutation()
        validate_snapshot(resource, fresh.observation, now(), target=target)
        if monotonic() - started > 60:
            raise ValueError("Decision expired during mutation verification.")
    except (ValueError, HTTPException, ApiException, *CONNECTION_ERRORS):
        return finish(
            "unavailable",
            "The runtime window or observations expired; no change was made.",
        )
    try:
        accepted = _patch(cluster, resource, proposed, annotations, runtime=target)
    except ApiException as error:
        state = (
            "unavailable"
            if error.status in (400, 401, 403, 404, 409, 422)
            else "unresolved"
        )
        return finish(
            state,
            "Kubernetes did not confirm the change; no write retry or restoration was attempted.",
        )
    except (*CONNECTION_ERRORS, ValueError):
        return finish(
            "unresolved",
            "The write outcome is unknown; inspect Iris before another run. No retry or restoration attempted.",
        )
    if not _identity(accepted, expected):
        return finish(
            "unresolved",
            "Kubernetes did not confirm the exact proposed revision; no restoration attempted.",
        )
    try:
        result["readiness"] = verify_revision(
            cluster, accepted, monotonic=monotonic, wait=wait, runtime=target
        )
        return finish(
            "applied",
            "The exact adjusted revision is ready and served a valid Iris prediction.",
        )
    except RolloutFailed:
        pass
    except (
        Superseded,
        ApiException,
        HTTPException,
        *CONNECTION_ERRORS,
        KeyError,
        TypeError,
        ValueError,
    ):
        return finish(
            "unresolved",
            "The accepted revision could not be verified or was superseded; no blind restoration attempted.",
        )
    try:
        current = cluster.resource(request_timeout=5)
        if not _identity(current, accepted):
            raise Superseded
        expected_restore = _expected(current, prior, annotations)
        if before_mutation:
            before_mutation()
        restored = _patch(cluster, current, prior, annotations, runtime=target)
        if not _identity(restored, expected_restore):
            raise Superseded
        result["recovery"] = verify_revision(
            cluster, restored, monotonic=monotonic, wait=wait, runtime=target
        )
        return finish(
            "restored",
            "Adjustment failed; captured prior settings were restored and verified.",
        )
    except (
        RolloutFailed,
        Superseded,
        ApiException,
        HTTPException,
        *CONNECTION_ERRORS,
        KeyError,
        TypeError,
        ValueError,
    ):
        return finish(
            "unresolved",
            "Adjustment failed and restoration could not be verified; inspect Iris and the saved prior settings.",
        )
