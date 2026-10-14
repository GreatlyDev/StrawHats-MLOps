"""Export the actual Iris pipeline; never read private workspace data."""

import argparse
import hashlib
import itertools
import json
import sys
from pathlib import Path

import sklearn

from backend.app import models

ROOT = Path(__file__).resolve().parents[2]
ARTIFACT = ROOT / "frontend/src/portfolio/iris-model.json"
ORACLE = ROOT / "frontend/tests/fixtures/iris-oracle.json"


def build_artifact() -> dict:
    pipeline, iris, metrics = models.fit_example()
    scaler, classifier = pipeline.steps[0][1], pipeline.steps[1][1]
    artifact = {
        "schema_version": 1,
        "id": "iris-classifier",
        "name": "Iris classifier",
        "version": "1.0.0",
        "feature_names": list(iris.feature_names),
        "labels": list(iris.target_names),
        "classes": classifier.classes_.tolist(),
        "scaler_mean": scaler.mean_.round(12).tolist(),
        "scaler_scale": scaler.scale_.round(12).tolist(),
        "coefficients": classifier.coef_.round(12).tolist(),
        "intercepts": classifier.intercept_.round(12).tolist(),
        "metrics": metrics,
        "training_split": {
            "train_samples": 120,
            "test_samples": 30,
            "random_state": 42,
            "stratified": True,
        },
        "provenance": {
            "algorithm": "StandardScaler + LogisticRegression",
            "scikit_learn": sklearn.__version__,
            "model_source_sha256": hashlib.sha256(
                Path(models.__file__).read_text(encoding="utf-8").encode()
            ).hexdigest(),
            "dataset_source": "https://scikit-learn.org/1.7/datasets/toy_dataset.html#iris-plants-dataset",
        },
    }
    artifact["content_sha256"] = hashlib.sha256(
        json.dumps(
            artifact, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()
    return artifact


def build_oracle() -> dict:
    pipeline, iris, _ = models.fit_example()
    vectors = (
        iris.data.tolist()
        + [
            [5.1, 3.5, 1.4, 0.2],
            [6.0, 2.9, 4.5, 1.5],
            [6.3, 3.3, 6.0, 2.5],
        ]
        + [list(row) for row in itertools.product((0.000001, 30.0), repeat=4)]
    )
    probabilities = pipeline.predict_proba(vectors)
    return {
        "cases": [
            {
                "features": vector,
                "label": str(iris.target_names[row.argmax()]),
                "probabilities": row.round(11).tolist(),
            }
            for vector, row in zip(vectors, probabilities, strict=True)
        ]
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    for path, value in ((ARTIFACT, build_artifact()), (ORACLE, build_oracle())):
        text = json.dumps(value, indent=2, ensure_ascii=True, allow_nan=False) + "\n"
        if args.check:
            if not path.is_file() or path.read_text(encoding="utf-8") != text:
                print(
                    f"Portfolio asset is stale: {path.name}. Regenerate the export.",
                    file=sys.stderr,
                )
                return 1
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
    print("Portfolio assets verified." if args.check else "Portfolio assets exported.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
