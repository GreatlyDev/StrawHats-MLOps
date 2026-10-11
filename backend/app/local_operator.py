"""Local-only Iris controls; the operator owns the underlying workloads."""

from ipaddress import ip_address

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from backend.model_operator.spec import choose_target, parse_target

from .local_cluster import CONTEXT, IMAGE, NAME, NAMESPACE, connect_local_cluster


class EmptyAction(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReplicaAction(EmptyAction):
    replicas: int = Field(ge=1, le=2, strict=True)


def snapshot(resource):
    try:
        return parse_target(resource.get("status", {}).get("lastReadySpec"))
    except (ValueError, AttributeError):
        return None


def observed_status(resource, operator_ready: bool) -> dict:
    result = {
        "enabled": True,
        "connected": True,
        "operator_ready": operator_ready,
        "context": CONTEXT,
        "namespace": NAMESPACE,
        "name": NAME,
        "phase": "NotDeployed",
        "mode": None,
        "image": IMAGE,
        "reported_image": None,
        "desired_replicas": None,
        "ready_replicas": 0,
        "generation": None,
        "observed_generation": None,
        "rollback_available": False,
        "message": "Iris has not been deployed in this local namespace.",
    }
    if resource:
        status, metadata = resource.get("status", {}), resource["metadata"]
        result.update(
            mode=resource["spec"].get("mode", "Deploy"),
            generation=metadata["generation"],
            observed_generation=status.get("observedGeneration"),
            ready_replicas=status.get("readyReplicas", 0),
            reported_image=status.get("image"),
            rollback_available=operator_ready and snapshot(resource) is not None,
        )
        try:
            target = choose_target(resource)
            result.update(image=target.image, desired_replicas=target.replicas)
        except ValueError:
            pass
        phase = status.get("phase", "Pending")
        result["phase"] = (
            phase
            if phase in ("Pending", "Ready", "RolledBack", "Failed", "Conflict")
            else "Pending"
        )
        result["message"] = next(
            (
                c.get("message", "")
                for c in status.get("conditions", [])
                if c.get("type") == "Ready"
            ),
            "Waiting for the operator to report status.",
        )
        if status.get("observedGeneration") != metadata["generation"]:
            result.update(
                phase="Pending",
                message="Waiting for the operator to observe the requested change.",
            )
        if metadata.get("deletionTimestamp"):
            result.update(phase="Unavailable", message="Local Iris is being deleted.")
    if not operator_ready:
        result.update(
            phase="Unavailable",
            message="The local operator is not ready. Check its deployment in strawhats-operator-local.",
        )
    return result


def local_operator_router(enabled: bool, record_event) -> APIRouter:
    router = APIRouter(prefix="/api/local-iris", tags=["Local Iris"])

    def guard(request: Request):
        if not enabled:
            raise HTTPException(503, "Local operator controls are disabled.")
        try:
            loopback = request.client and ip_address(request.client.host).is_loopback
        except ValueError:
            loopback = False
        if not loopback or request.url.hostname not in (
            "127.0.0.1",
            "localhost",
            "::1",
        ):
            raise HTTPException(
                403, "Local Iris controls require a loopback caller and API host."
            )
        origin = request.headers.get("origin")
        if origin and origin not in (
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            str(request.base_url).rstrip("/"),
        ):
            raise HTTPException(
                403, "This browser origin cannot use local Iris controls."
            )
        if (
            request.method == "POST"
            and request.headers.get("content-type", "").split(";")[0]
            != "application/json"
        ):
            raise HTTPException(415, "Local Iris actions require application/json.")

    @router.get("")
    def status(request: Request):
        if enabled:
            guard(request)
            with connect_local_cluster() as cluster:
                ready = cluster.controller_ready()
                return observed_status(cluster.resource(), ready)
        return {
            "enabled": False,
            "connected": False,
            "operator_ready": False,
            "context": CONTEXT,
            "namespace": NAMESPACE,
            "name": NAME,
            "phase": "Disabled",
            "mode": None,
            "image": IMAGE,
            "reported_image": None,
            "desired_replicas": None,
            "ready_replicas": 0,
            "generation": None,
            "observed_generation": None,
            "rollback_available": False,
            "message": "Enable LOCAL_OPERATOR_ENABLED=1 in the local backend environment to use Docker Desktop controls.",
        }

    def apply(action: str, replicas: int | None = None):
        with connect_local_cluster() as cluster:
            if not cluster.controller_ready():
                raise HTTPException(
                    503, "Start the local operator before changing Iris."
                )
            resource = cluster.resource()
            if action == "deploy":
                spec = {
                    "mode": "Deploy",
                    "image": IMAGE,
                    "imagePullPolicy": "Never",
                    "replicas": replicas,
                    "cpuMillicores": 250,
                    "memoryMi": 256,
                }
            elif action == "scale":
                if observed_status(resource, True)["phase"] not in (
                    "Ready",
                    "RolledBack",
                ):
                    raise HTTPException(
                        409,
                        "Scaling requires a verified ready Iris deployment. Refresh or roll back first.",
                    )
                spec = {
                    **choose_target(resource).to_spec(),
                    "mode": "Deploy",
                    "replicas": replicas,
                }
            else:
                if snapshot(resource) is None:
                    raise HTTPException(
                        409, "Rollback requires a previously verified ready snapshot."
                    )
                spec = {"mode": "Rollback"}
            changed = cluster.apply(resource, spec)
            result = observed_status(changed, True)
        record_event(
            "deployment",
            f"Local Iris {action} requested",
            f"{NAMESPACE}/{NAME} · {result['desired_replicas']} replica(s) · request accepted",
        )
        return result

    @router.post("/deploy", status_code=202, dependencies=[Depends(guard)])
    def deploy(data: ReplicaAction):
        return apply("deploy", data.replicas)

    @router.post("/scale", status_code=202, dependencies=[Depends(guard)])
    def scale(data: ReplicaAction):
        return apply("scale", data.replicas)

    @router.post("/rollback", status_code=202, dependencies=[Depends(guard)])
    def rollback(data: EmptyAction):
        return apply("rollback")

    return router
