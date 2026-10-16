"""Private, factory-bound Iris controls; the operator owns workloads."""

import json
from ipaddress import ip_address

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from backend.model_operator.spec import choose_target, parse_target

from .iris_target import LOCAL_TARGET
from .local_inference import DeployedPredictionInput, predict_with_service


class EmptyAction(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReplicaAction(EmptyAction):
    replicas: int = Field(ge=1, le=2, strict=True)


def snapshot(resource):
    try:
        return parse_target(resource.get("status", {}).get("lastReadySpec"))
    except (ValueError, AttributeError):
        return None


def observed_status(resource, operator_ready: bool, *, target=LOCAL_TARGET) -> dict:
    result = {
        "enabled": True,
        "connected": True,
        "operator_ready": operator_ready,
        "context": target.context,
        "namespace": target.namespace,
        "name": target.name,
        "phase": "NotDeployed",
        "mode": None,
        "image": target.image,
        "reported_image": None,
        "desired_replicas": None,
        "ready_replicas": 0,
        "generation": None,
        "observed_generation": None,
        "rollback_available": False,
        "message": "Iris has not been deployed in the selected namespace.",
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
            effective = choose_target(resource)
            result.update(image=effective.image, desired_replicas=effective.replicas)
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
            result.update(phase="Unavailable", message="Iris is being deleted.")
    if not operator_ready:
        result.update(
            phase="Unavailable",
            message=f"The operator is not ready. Check {target.namespace}/{target.operator_name}.",
        )
    if resource and target.kind != "local":
        try:
            effective = choose_target(resource)
            if (
                effective.image != target.image
                or effective.image_pull_policy != target.image_pull_policy
            ):
                raise ValueError("Unbound release image")
            saved = snapshot(resource)
            result["rollback_available"] = bool(
                operator_ready
                and saved
                and saved.image == target.image
                and saved.image_pull_policy == target.image_pull_policy
            )
        except ValueError:
            result.update(
                phase="Conflict",
                rollback_available=False,
                message="Iris does not match the selected release image.",
            )
    return result


def iris_router(
    *, prefix: str, label: str, enabled: bool, connect, record_event
) -> APIRouter:
    if prefix not in ("/api/local-iris", "/api/eks-iris", "/api/rehearsal-iris"):
        raise ValueError("Unsupported private Iris route.")
    local = prefix == "/api/local-iris"
    router = APIRouter(prefix=prefix, tags=[label])

    def bound_resource(connection):
        resource = connection.cluster.resource()
        if connection.target.kind != "local":
            records = [
                r
                for r in connection.receipt.inventory
                if r.kind == "ModelDeployment"
                and r.name == connection.target.name
                and r.namespace == connection.target.namespace
            ]
            metadata = (resource or {}).get("metadata", {})
            if (
                len(records) != 1
                or metadata.get("uid") != records[0].uid
                or metadata.get("namespace") != connection.target.namespace
                or metadata.get("name") != connection.target.name
            ):
                raise HTTPException(
                    503, "The selected Iris release identity is unavailable."
                )
        return resource

    def guard(request: Request):
        if not enabled:
            raise HTTPException(503, f"{label} controls are disabled.")
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
                403, "Private Iris controls require a loopback caller and API host."
            )
        origin = request.headers.get("origin")
        if origin and origin not in (
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            str(request.base_url).rstrip("/"),
        ):
            raise HTTPException(
                403, "This browser origin cannot use private Iris controls."
            )
        if (
            request.method == "POST"
            and request.headers.get("content-type", "").split(";")[0]
            != "application/json"
        ):
            raise HTTPException(415, "Private Iris actions require application/json.")

    @router.get("")
    def status(request: Request):
        if enabled:
            guard(request)
            with connect() as connection:
                cluster, target = connection.cluster, connection.target
                ready = cluster.controller_ready()
                return observed_status(bound_resource(connection), ready, target=target)
        return {
            "enabled": False,
            "connected": False,
            "operator_ready": False,
            "context": LOCAL_TARGET.context if local else None,
            "namespace": LOCAL_TARGET.namespace if local else "strawhats",
            "name": "iris",
            "phase": "Disabled",
            "mode": None,
            "image": LOCAL_TARGET.image if local else None,
            "reported_image": None,
            "desired_replicas": None,
            "ready_replicas": 0,
            "generation": None,
            "observed_generation": None,
            "rollback_available": False,
            "message": "Enable LOCAL_OPERATOR_ENABLED=1 in the local backend environment to use Docker Desktop controls."
            if local
            else "Select and configure this owner runtime in the private backend environment.",
        }

    def apply(action: str, replicas: int | None = None):
        with connect() as connection:
            cluster, target = connection.cluster, connection.target
            if not cluster.controller_ready():
                raise HTTPException(
                    503, "Start the selected operator before changing Iris."
                )
            resource = bound_resource(connection)
            if action == "deploy":
                spec = {
                    "mode": "Deploy",
                    "image": target.image,
                    "imagePullPolicy": target.image_pull_policy,
                    "replicas": replicas,
                    "cpuMillicores": 250,
                    "memoryMi": 256,
                }
            elif action == "scale":
                if observed_status(resource, True, target=target)["phase"] not in (
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
                if snapshot(resource) is None or (
                    target.kind != "local"
                    and not observed_status(resource, True, target=target)[
                        "rollback_available"
                    ]
                ):
                    raise HTTPException(
                        409, "Rollback requires a previously verified ready snapshot."
                    )
                spec = {"mode": "Rollback"}
            changed = cluster.apply(resource, spec)
            result = observed_status(changed, True, target=target)
        record_event(
            "deployment",
            f"{label} {action} requested",
            f"{target.context} · {target.namespace}/{target.name} · {result['desired_replicas']} replica(s) · request accepted",
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

    async def prediction_guard(request: Request):
        guard(request)
        # FastAPI echoes invalid inputs in validation errors; NaN/Inf cannot be
        # serialized there. Reject non-finite JSON before model validation.
        try:
            json.dumps(await request.json(), allow_nan=False)
        except ValueError:
            raise HTTPException(
                422, "Prediction measurements must be finite JSON numbers."
            ) from None

    @router.post("/predict", dependencies=[Depends(prediction_guard)])
    def predict(data: DeployedPredictionInput):
        with connect() as connection:
            cluster, target = connection.cluster, connection.target
            if not cluster.controller_ready():
                raise HTTPException(
                    503, "Start the selected operator before predicting with Iris."
                )
            resource = bound_resource(connection)
            status = observed_status(resource, True, target=target)
            if (
                status["phase"] not in ("Ready", "RolledBack")
                or status["ready_replicas"] < 1
            ):
                raise HTTPException(
                    409,
                    "Prediction requires a current ready Iris deployment. Refresh or deploy first.",
                )
            result = {
                **predict_with_service(cluster, resource, data, target=target),
                "source": "kubernetes",
                "context": target.context,
                "namespace": target.namespace,
                "service": target.name,
                "image": status["image"],
                "generation": status["generation"],
            }
        record_event(
            "prediction",
            "Kubernetes Iris prediction completed",
            f"{target.context} · {target.namespace}/{target.name} · generation {result['generation']} · {result['label']}",
        )
        return result

    return router
