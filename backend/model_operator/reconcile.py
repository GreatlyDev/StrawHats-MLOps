"""One idempotent lifecycle iteration, independent of Kubernetes transport."""

from copy import deepcopy
from typing import Protocol

from kubernetes.utils.quantity import parse_quantity

from .spec import choose_target
from .workload import render_workload


class ClusterAPI(Protocol):
    def get_deployment(self, name: str) -> dict | None: ...
    def get_service(self, name: str) -> dict | None: ...
    def list_replica_sets(self, selector: str) -> list[dict]: ...
    def list_pods(self, selector: str) -> list[dict]: ...
    def list_endpoint_slices(self, service: str) -> list[dict]: ...
    def create_deployment(self, body: dict) -> None: ...
    def patch_deployment(self, name: str, body: dict) -> None: ...
    def create_service(self, body: dict) -> None: ...
    def patch_service(self, name: str, body: dict) -> None: ...
    def patch_status(self, name: str, resource_version: str, status: dict) -> None: ...


def _owned(body: dict, uid: str, kind: str) -> bool:
    return any(
        owner.get("uid") == uid
        and owner.get("kind") == kind
        and owner.get("controller") is True
        for owner in body.get("metadata", {}).get("ownerReferences", [])
    )


def _matches(actual, wanted, key="") -> bool:
    """Compare managed fields, ignoring API defaults and equivalent quantities."""
    if isinstance(wanted, dict):
        return isinstance(actual, dict) and all(
            _matches(actual.get(k), v, k) for k, v in wanted.items()
        )
    if isinstance(wanted, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(wanted)
            and all(_matches(a, w) for a, w in zip(actual, wanted))
        )
    if key in ("cpu", "memory") and isinstance(actual, str):
        return parse_quantity(actual) == parse_quantity(wanted)
    return actual == wanted


def _status(
    resource, api, phase, reason, message, deployment=None, ready=0, snapshot=None
):
    previous = resource.get("status", {})
    status = {
        "observedGeneration": resource["metadata"]["generation"],
        "phase": phase,
        "replicas": (deployment or {}).get("status", {}).get("replicas", 0),
        "readyReplicas": ready,
        "image": (
            (deployment or {})
            .get("spec", {})
            .get("template", {})
            .get("spec", {})
            .get("containers", [{}])[0]
            .get("image", "")
        ),
        "selector": f"mlops.strawhats.dev/owner-uid={resource['metadata']['uid']}",
        "conditions": [
            {
                "type": "Ready",
                "status": "True" if phase in ("Ready", "RolledBack") else "False",
                "reason": reason,
                "message": message,
            }
        ],
    }
    saved = snapshot or previous.get("lastReadySpec")
    if saved:
        status["lastReadySpec"] = deepcopy(saved)
    if status != previous:
        api.patch_status(
            resource["metadata"]["name"],
            resource["metadata"]["resourceVersion"],
            status,
        )


def reconcile(resource: dict, api: ClusterAPI) -> None:
    if resource["metadata"].get("deletionTimestamp"):
        return
    try:
        target = choose_target(resource)
    except ValueError as error:
        _status(resource, api, "Failed", "InvalidConfiguration", str(error))
        return
    name, uid = resource["metadata"]["name"], resource["metadata"]["uid"]
    desired_deployment, desired_service = render_workload(resource, target)
    deployment, service = api.get_deployment(name), api.get_service(name)
    for child in (deployment, service):
        if child and (
            not _owned(child, uid, "ModelDeployment")
            or child["metadata"].get("deletionTimestamp")
        ):
            _status(
                resource,
                api,
                "Conflict",
                "OwnershipConflict",
                "A same-name child is foreign or terminating; no children changed",
            )
            return
    changed = False
    for current, wanted, create, patch in (
        (deployment, desired_deployment, api.create_deployment, api.patch_deployment),
        (service, desired_service, api.create_service, api.patch_service),
    ):
        if current is None:
            create(wanted)
            changed = True
        elif not _matches(current, wanted):
            body = deepcopy(wanted)
            if current["metadata"].get("resourceVersion"):
                body["metadata"]["resourceVersion"] = current["metadata"][
                    "resourceVersion"
                ]
            patch(name, body)
            changed = True
    if changed:
        _status(
            resource,
            api,
            "Pending",
            "Reconciling",
            "Waiting for changed resources",
            deployment,
        )
        return

    selector = f"mlops.strawhats.dev/owner-uid={uid}"
    template = desired_deployment["spec"]["template"]
    current_sets = {
        rs["metadata"]["uid"]
        for rs in api.list_replica_sets(selector)
        if _owned(rs, deployment["metadata"]["uid"], "Deployment")
        and not rs["metadata"].get("deletionTimestamp")
        and _matches(rs.get("spec", {}).get("template"), template)
    }
    current_pods = [
        pod
        for pod in api.list_pods(selector)
        if any(_owned(pod, rs_uid, "ReplicaSet") for rs_uid in current_sets)
        and not pod["metadata"].get("deletionTimestamp")
        and _matches(pod.get("spec"), template["spec"])
    ]
    ready_pods = {
        pod["metadata"]["uid"]
        for pod in current_pods
        if pod.get("status", {}).get("phase") == "Running"
        and any(
            c.get("type") == "Ready" and c.get("status") == "True"
            for c in pod.get("status", {}).get("conditions", [])
        )
    }
    endpoint_uids = {
        endpoint.get("targetRef", {}).get("uid")
        for slice_ in api.list_endpoint_slices(name)
        if any(
            p.get("port") == 8000 and p.get("protocol", "TCP") == "TCP"
            for p in slice_.get("ports", [])
        )
        for endpoint in slice_.get("endpoints", [])
        if endpoint.get("conditions", {}).get("ready") is True
        and not endpoint.get("conditions", {}).get("terminating", False)
        and endpoint.get("targetRef", {}).get("kind") == "Pod"
    }
    observed = deployment.get("status", {})
    generation_observed = (
        observed.get("observedGeneration", 0) >= deployment["metadata"]["generation"]
    )
    ready = len(ready_pods & endpoint_uids)
    complete = (
        generation_observed
        and ready == target.replicas
        and all(
            observed.get(field, 0) == target.replicas
            for field in (
                "replicas",
                "updatedReplicas",
                "readyReplicas",
                "availableReplicas",
            )
        )
    )
    if complete:
        phase = "RolledBack" if resource["spec"].get("mode") == "Rollback" else "Ready"
        _status(
            resource,
            api,
            phase,
            "RolloutVerified",
            "Requested revision and endpoints are ready",
            deployment,
            ready,
            target.to_spec(),
        )
        return

    failure = None
    for pod in current_pods:
        for container in pod.get("status", {}).get("containerStatuses", []):
            reason = container.get("state", {}).get("waiting", {}).get("reason")
            if reason in (
                "ErrImageNeverPull",
                "InvalidImageName",
                "ImagePullBackOff",
                "ErrImagePull",
                "CrashLoopBackOff",
                "CreateContainerConfigError",
            ):
                failure = reason
    if generation_observed and any(
        c.get("type") == "Progressing"
        and c.get("status") == "False"
        and c.get("reason") == "ProgressDeadlineExceeded"
        for c in observed.get("conditions", [])
    ):
        failure = "ProgressDeadlineExceeded"
    _status(
        resource,
        api,
        "Failed" if failure else "Pending",
        failure or "WaitingForReadiness",
        "Requested rollout failed"
        if failure
        else "Waiting for current Pods and Service endpoints",
        deployment,
        ready,
    )
