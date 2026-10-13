import json
import math

MAX_CONTEXT_MODELS = 10
MAX_CONTEXT_JSON_CHARS = 12000
_MAX_INTEGER = 2**63 - 1

_MODEL_STRING_LIMITS = {
    "id": 100,
    "name": 80,
    "version": 40,
    "framework": 50,
    "kind": 20,
}
_METRIC_RANGES = {
    "accuracy": (0, 1),
    "f1_macro": (0, 1),
    "train_accuracy": (0, 1),
}
_SAMPLE_METRICS = {"train_samples", "test_samples"}
_WORKSPACE_COUNTS = {"models", "deployment_plans", "predictions"}


def _finite_number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if math.isfinite(value):
            return value
    except OverflowError:
        return None
    return None


def _valid_count(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= _MAX_INTEGER
    )


def _metrics(value: object) -> dict:
    if not isinstance(value, dict):
        return {}

    result = {}
    for name, bounds in _METRIC_RANGES.items():
        number = _finite_number(value.get(name))
        if number is not None and bounds[0] <= number <= bounds[1]:
            result[name] = number

    for name in _SAMPLE_METRICS:
        samples = value.get(name)
        if _valid_count(samples):
            result[name] = samples
    return result


def _model_summary(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None

    summary = {
        name: field[: _MODEL_STRING_LIMITS[name]]
        for name in _MODEL_STRING_LIMITS
        if isinstance((field := value.get(name)), str)
    }
    summary["metrics"] = _metrics(value.get("metrics", {}))
    return summary


def _workspace(overview: dict) -> dict:
    workspace = {
        name: overview[name]
        for name in _WORKSPACE_COUNTS
        if _valid_count(overview.get(name))
    }
    latency = _finite_number(overview.get("median_latency_ms"))
    workspace["median_latency_ms"] = (
        latency if latency is not None and latency >= 0 else None
    )
    return workspace


def build_workspace_context(models: list[dict], overview: dict) -> dict:
    total = len(models)
    summaries = []
    for model in models:
        summary = _model_summary(model)
        if summary is not None:
            summaries.append(summary)
        if len(summaries) == MAX_CONTEXT_MODELS:
            break

    context = {
        "models": summaries,
        "model_summary": {
            "total": total,
            "included": len(summaries),
            "omitted": total - len(summaries),
        },
        "workspace": _workspace(overview),
        "capabilities": [
            "model registration",
            "local Iris inference",
            "Kubernetes deployment previews",
        ],
        "limitations": [
            "Cluster CPU/memory, live workload status, deployed endpoints, and drift measurements are unavailable in this context.",
            "Resource advice is provisional and cannot apply infrastructure changes.",
            "A high prediction count alone does not measure throughput or CPU utilization, prove drift, or justify automatic scaling.",
        ],
    }

    while (
        len(json.dumps(context, ensure_ascii=True, allow_nan=False, sort_keys=True))
        > MAX_CONTEXT_JSON_CHARS
    ):
        if not summaries:
            raise ValueError(
                "Fixed assistant context exceeds its JSON character budget."
            )
        summaries.pop()
        context["model_summary"] = {
            "total": total,
            "included": len(summaries),
            "omitted": total - len(summaries),
        }

    return context
