"""Validate inputs and results for inference through the fixed local Iris Service."""

from collections.abc import Callable
from math import isclose
from typing import Annotated, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .iris_target import LOCAL_TARGET, RuntimeTarget
from .local_cluster import GROUP, TIMEOUT, VERSION

Measurement = Annotated[float, Field(gt=0, le=30, allow_inf_nan=False, strict=True)]
Probability = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False, strict=True)]
IrisLabel = Literal["setosa", "versicolor", "virginica"]


class DeployedPredictionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    features: list[Measurement] = Field(min_length=4, max_length=4)


class IrisResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: IrisLabel
    confidence: Probability
    probabilities: dict[IrisLabel, Probability]
    latency_ms: float = Field(gt=0, allow_inf_nan=False, strict=True)

    @model_validator(mode="after")
    def consistent_probabilities(self):
        values = self.probabilities
        if (
            set(values) != {"setosa", "versicolor", "virginica"}
            or not isclose(sum(values.values()), 1, abs_tol=1e-6)
            or not isclose(self.confidence, values[self.label], abs_tol=1e-6)
            or values[self.label] < max(values.values())
        ):
            raise ValueError("Invalid Iris probabilities")
        return self


def predict_with_service(
    cluster,
    resource,
    data: DeployedPredictionInput,
    *,
    before_request: Callable[[], None] | None = None,
    request_timeout=TIMEOUT,
    target: RuntimeTarget = LOCAL_TARGET,
) -> dict:
    from kubernetes import client

    if (
        resource.get("metadata", {}).get("namespace") != target.namespace
        or resource.get("metadata", {}).get("name") != target.name
    ):
        raise HTTPException(409, "Iris identity does not match this runtime.")
    if before_request:
        before_request()
    service = cluster.sdk.sanitize_for_serialization(
        client.CoreV1Api(cluster.sdk).read_namespaced_service(
            target.name,
            target.namespace,
            _request_timeout=request_timeout()
            if callable(request_timeout)
            else request_timeout,
        )
    )
    uid = resource["metadata"]["uid"]
    spec = service.get("spec", {})
    owned = any(
        owner.get("uid") == uid
        and owner.get("kind") == "ModelDeployment"
        and owner.get("name") == target.name
        and owner.get("apiVersion") == f"{GROUP}/{VERSION}"
        and owner.get("controller") is True
        for owner in service.get("metadata", {}).get("ownerReferences", [])
    )
    if (
        not owned
        or spec.get("type") != "ClusterIP"
        or spec.get("selector") != {"mlops.strawhats.dev/owner-uid": uid}
        or not any(
            port.get("port") == 8000
            and port.get("targetPort") == "http"
            and port.get("protocol") == "TCP"
            for port in spec.get("ports", [])
        )
    ):
        raise HTTPException(
            409,
            "The local Iris Service is not the operator's verified target. Refresh and retry.",
        )
    # Generated Service proxy methods omit the body; use the SDK's JSON transport.
    try:
        if before_request:
            before_request()
        result = cluster.sdk.call_api(
            "/api/v1/namespaces/{namespace}/services/{name}/proxy/{path}",
            "POST",
            path_params={
                "namespace": target.namespace,
                "name": f"{target.name}:8000",
                "path": "predict",
            },
            header_params={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            body=data.model_dump(),
            response_type="object",
            auth_settings=["BearerToken"],
            _return_http_data_only=True,
            _request_timeout=request_timeout()
            if callable(request_timeout)
            else request_timeout,
        )
        return IrisResult.model_validate(result).model_dump()
    except (ValidationError, UnicodeDecodeError):
        raise HTTPException(
            502,
            "The deployed Iris Service returned an invalid prediction. No result was substituted.",
        ) from None
