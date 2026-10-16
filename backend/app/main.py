import os
import sqlite3
from pathlib import Path
from threading import Lock
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)
from starlette.responses import Response

from .assistant import AssistantUnavailable, answer
from .deployments import cluster_status, preview
from .iris_routes import iris_router
from .local_operator import local_operator_router
from .models import predict_example, train_example
from .owner_target import owner_connection
from .schemas import AssistantInput, DeploymentInput, PredictionInput, RegisterModel
from .store import Store, now

ROOT = Path(__file__).resolve().parents[2]


def create_app(data_dir: Path | None = None, test_mode: bool = False) -> FastAPI:
    if not test_mode:
        load_dotenv(ROOT / ".env.local", override=False)
    directory = data_dir or ROOT / ".local"
    store = Store(directory)
    key = None if test_mode else os.getenv("OPENAI_API_KEY")
    ai_model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
    model_lock = Lock()
    registry = CollectorRegistry()
    predictions = Counter(
        "strawhats_predictions_total",
        "Successful local predictions since process start",
        ["model_id"],
        registry=registry,
    )
    latency = Histogram(
        "strawhats_prediction_seconds",
        "Local prediction latency",
        ["model_id"],
        registry=registry,
    )
    app = FastAPI(title="StrawHats MLOps", version="0.1.0")
    origins = os.getenv(
        "FRONTEND_ORIGINS", "http://127.0.0.1:5173,http://localhost:5173"
    ).split(",")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[origin.strip() for origin in origins],
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )
    app.include_router(
        local_operator_router(
            enabled=not test_mode and os.getenv("LOCAL_OPERATOR_ENABLED") == "1",
            record_event=store.event,
        )
    )
    owner_kind = "disabled" if test_mode else os.getenv("OWNER_CLUSTER_TARGET", "local")
    owner_config = {
        name: os.getenv(name, "")
        for name in (
            "EKS_AWS_PROFILE",
            "EKS_EXPECTED_ACCOUNT",
            "EKS_RELEASE_RECEIPT",
            "REHEARSAL_RECEIPT",
        )
    }
    descriptions = {
        "local": (
            "/local-iris",
            "Local Iris",
            "Deploy and manage Iris on Docker Desktop Kubernetes.",
        ),
        "eks": (
            "/eks-iris",
            "AWS EKS Iris",
            "Manage the verified owner release on AWS Kubernetes.",
        ),
        "rehearsal": (
            "/rehearsal-iris",
            "Rehearsal Iris",
            "Manage the dedicated local release rehearsal on Docker Desktop.",
        ),
    }
    for kind in ("eks", "rehearsal"):
        prefix, label, _ = descriptions[kind]
        app.include_router(
            iris_router(
                prefix="/api" + prefix,
                label=label,
                enabled=owner_kind == kind,
                connect=lambda kind=kind: owner_connection(kind, owner_config),
                record_event=store.event,
            )
        )

    @app.get("/api/owner-target")
    def owner_target():
        if owner_kind not in descriptions:
            return {
                "kind": "disabled",
                "api_prefix": None,
                "title": "Cluster controls unavailable",
                "description": "A configured private owner workspace is required.",
            }
        prefix, title, description = descriptions[owner_kind]
        return {
            "kind": owner_kind,
            "api_prefix": prefix,
            "title": title,
            "description": description,
        }

    def find_model(model_id: str) -> dict:
        model = store.model(model_id)
        if model is None:
            raise HTTPException(404, "Model not found in this workspace.")
        return model

    @app.get("/health", include_in_schema=False)
    @app.get("/api/health")
    def health():
        return {
            "status": "ok",
            "ai_configured": bool(key),
            "ai_model": ai_model,
            "version": "0.1.0",
        }

    @app.get("/api/models")
    def list_models():
        return store.models()

    @app.post("/api/models/example", status_code=201)
    def create_example():
        with model_lock:
            if store.model("iris-classifier"):
                raise HTTPException(409, "The Iris example is already registered.")
            if store.has_identity("Iris classifier", "1.0.0"):
                raise HTTPException(
                    409,
                    "The example name and version are already used by a container record. Use another container identity in a new workspace.",
                )
            model = train_example(directory)
            store.add_model(model)
            return model

    @app.post("/api/models", status_code=201)
    def register_model(config: RegisterModel):
        if config.name == "Iris classifier" and config.version == "1.0.0":
            raise HTTPException(
                409,
                "Iris classifier v1.0.0 is reserved for the built-in example. Choose another container name or version.",
            )
        model = {
            "id": f"model-{uuid4().hex[:12]}",
            **config.model_dump(),
            "created_at": now(),
            "kind": "container",
            "metrics": {},
            "feature_names": [],
            "labels": [],
        }
        try:
            store.add_model(model)
        except sqlite3.IntegrityError:
            raise HTTPException(
                409, "A model with that name and version is already registered."
            ) from None
        return model

    @app.post("/api/models/{model_id}/predict")
    def predict(model_id: str, data: PredictionInput):
        model = find_model(model_id)
        if model["kind"] != "local":
            raise HTTPException(
                422,
                "Local prediction is available for the Iris example. Container models are served by their deployed endpoint.",
            )
        try:
            result = predict_example(directory, data.features, model["labels"])
        except FileNotFoundError:
            raise HTTPException(
                409, "The trained artifact is missing from this workspace."
            ) from None
        store.prediction(model_id, result["label"], result["latency_ms"])
        predictions.labels(model_id=model_id).inc()
        latency.labels(model_id=model_id).observe(result["latency_ms"] / 1000)
        return result

    @app.get("/api/models/{model_id}/prediction-summary")
    def prediction_summary(model_id: str):
        find_model(model_id)
        return store.prediction_summary(model_id)

    @app.post("/api/deployment-plans", status_code=201)
    def create_plan(config: DeploymentInput):
        find_model(config.model_id)
        plan = preview(config)
        store.add_plan(plan)
        return plan

    @app.get("/api/deployment-plans")
    def list_plans():
        return store.plans()

    @app.get("/api/overview")
    def overview():
        return store.overview()

    @app.get("/api/events")
    def events():
        return store.events()

    @app.get("/api/cluster")
    def cluster():
        return cluster_status(enabled=not test_mode)

    @app.post("/api/assistant")
    def assistant(data: AssistantInput):
        try:
            message = answer(
                [m.model_dump() for m in data.messages],
                store.models(),
                store.overview(),
                key,
                ai_model,
            )
        except AssistantUnavailable as exc:
            raise HTTPException(503, str(exc)) from None
        store.event(
            "assistant",
            "Assistant responded",
            "Deployment guidance generated from workspace context",
        )
        return {"message": message, "model": ai_model}

    @app.get("/metrics", include_in_schema=False)
    def metrics():
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    return app
