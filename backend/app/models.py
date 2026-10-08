from pathlib import Path
from time import perf_counter

import joblib
from sklearn.datasets import load_iris
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, f1_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from .store import now


def fit_example():
    iris = load_iris()
    train_x, test_x, train_y, test_y = train_test_split(
        iris.data, iris.target, test_size=0.2, random_state=42, stratify=iris.target
    )
    pipeline = make_pipeline(
        StandardScaler(), LogisticRegression(max_iter=500, random_state=42)
    )
    pipeline.fit(train_x, train_y)
    metrics = {
        "accuracy": float(accuracy_score(test_y, pipeline.predict(test_x))),
        "f1_macro": float(f1_score(test_y, pipeline.predict(test_x), average="macro")),
        "train_accuracy": float(accuracy_score(train_y, pipeline.predict(train_x))),
        "train_samples": len(train_y),
        "test_samples": len(test_y),
    }
    return pipeline, iris, metrics


def train_example(directory: Path) -> dict:
    pipeline, iris, metrics = fit_example()
    (directory / "models").mkdir(parents=True, exist_ok=True)
    joblib.dump(pipeline, directory / "models" / "iris-classifier.joblib")
    return {
        "id": "iris-classifier",
        "name": "Iris classifier",
        "version": "1.0.0",
        "framework": "scikit-learn",
        "kind": "local",
        "image": None,
        "description": "Logistic regression on the Iris dataset. A reproducible starter model for testing the model lifecycle.",
        "created_at": now(),
        "metrics": metrics,
        "feature_names": list(iris.feature_names),
        "labels": list(iris.target_names),
    }


def predict_example(directory: Path, features: list[float], labels: list[str]) -> dict:
    started = perf_counter()
    pipeline = joblib.load(directory / "models" / "iris-classifier.joblib")
    probabilities = pipeline.predict_proba([features])[0]
    index = int(probabilities.argmax())
    return {
        "label": labels[index],
        "confidence": float(probabilities[index]),
        "probabilities": dict(
            zip(labels, [float(p) for p in probabilities], strict=True)
        ),
        "latency_ms": max((perf_counter() - started) * 1000, 0.001),
    }
