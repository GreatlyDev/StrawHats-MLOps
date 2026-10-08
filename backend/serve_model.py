from time import perf_counter

from fastapi import FastAPI
from fastapi.responses import Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)

from backend.app.models import fit_example
from backend.app.schemas import PredictionInput


def create_model_service() -> FastAPI:
    """Standalone model workload satisfying the generated Kubernetes manifest."""
    pipeline, iris, metrics = fit_example()
    registry = CollectorRegistry()
    predictions = Counter(
        "strawhats_model_predictions_total",
        "Successful model predictions",
        registry=registry,
    )
    duration = Histogram(
        "strawhats_model_prediction_seconds",
        "Model inference duration",
        registry=registry,
    )
    app = FastAPI(title="StrawHats Iris model service", version="0.1.0")

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "model": "iris-classifier",
            "version": "1.0.0",
            "metrics": metrics,
        }

    @app.post("/predict")
    def predict(payload: PredictionInput):
        started = perf_counter()
        probabilities = pipeline.predict_proba([payload.features])[0]
        index = int(probabilities.argmax())
        elapsed = perf_counter() - started
        predictions.inc()
        duration.observe(elapsed)
        return {
            "label": str(iris.target_names[index]),
            "confidence": float(probabilities[index]),
            "probabilities": dict(
                zip(map(str, iris.target_names), map(float, probabilities), strict=True)
            ),
            "latency_ms": max(elapsed * 1000, 0.001),
        }

    @app.get("/metrics")
    def prometheus_metrics():
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    return app
