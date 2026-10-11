"""Check real Iris HTTP responses; a failed check never produces success evidence."""

import argparse
import json
import math
import re
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def _request(method, url, timeout, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    request = Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode()
    except HTTPError as exc:
        return exc.code, exc.read().decode()
    except (URLError, OSError, UnicodeError) as exc:
        raise RuntimeError(f"Iris endpoint could not be reached: {url}") from exc


def _counter(text):
    match = re.search(r"^strawhats_model_predictions_total\s+(\S+)", text, re.M)
    try:
        value = float(match[1]) if match else float("nan")
    except ValueError as exc:
        raise RuntimeError("Prediction counter is malformed.") from exc
    if not math.isfinite(value) or value < 0:
        raise RuntimeError("A finite successful-prediction counter is required.")
    return value


def verify_iris(base_url: str, timeout_seconds: float = 10) -> dict:
    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise RuntimeError("Use an HTTP(S) base URL without credentials and a timeout.")
    base_url = base_url.rstrip("/")

    def get(path):
        status, body = _request("GET", base_url + path, timeout_seconds)
        if status != 200:
            raise RuntimeError(f"{path} returned HTTP {status}.")
        return body

    try:
        health = json.loads(get("/health"))
        if health.get("status") != "ok" or health.get("model") != "iris-classifier":
            raise RuntimeError("Health response does not identify a ready Iris model.")
        before = _counter(get("/metrics"))
        status, body = _request(
            "POST",
            base_url + "/predict",
            timeout_seconds,
            {"features": [5.1, 3.5, 1.4, 0.2]},
        )
        if status != 200:
            raise RuntimeError(f"Prediction returned HTTP {status}.")
        prediction = json.loads(body)
        confidence = float(prediction["confidence"])
        probabilities = prediction["probabilities"]
        if (
            prediction["label"] != "setosa"
            or not math.isfinite(confidence)
            or not 0.8 < confidence <= 1
            or set(probabilities) != {"setosa", "versicolor", "virginica"}
            or any(
                not math.isfinite(p) or not 0 <= p <= 1 for p in probabilities.values()
            )
            or not math.isclose(sum(probabilities.values()), 1, abs_tol=1e-6)
            or not math.isclose(probabilities["setosa"], confidence, abs_tol=1e-6)
            or not math.isfinite(prediction["latency_ms"])
            or prediction["latency_ms"] <= 0
        ):
            raise RuntimeError(
                "Prediction does not contain valid measured Iris output."
            )
        after = _counter(get("/metrics"))
        if after <= before:
            raise RuntimeError("The successful-prediction counter did not increase.")
        invalid_status, _ = _request(
            "POST",
            base_url + "/predict",
            timeout_seconds,
            {"features": [0, 3.5, 1.4, 0.2]},
        )
        if invalid_status != 422:
            raise RuntimeError("The model did not reject invalid input with HTTP 422.")
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("The endpoint returned malformed model evidence.") from exc
    return {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "health": health,
        "prediction": prediction,
        "counter_before": before,
        "counter_after": after,
        "invalid_input_status": invalid_status,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=10)
    args = parser.parse_args()
    try:
        evidence = verify_iris(args.base_url, args.timeout_seconds)
    except RuntimeError as exc:
        parser.exit(1, f"Verification failed: {exc}\n")
    print(json.dumps(evidence, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
