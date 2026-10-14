import hashlib
import importlib
import json

import numpy as np
import pytest

from backend.app.models import fit_example


def load_exporter():
    try:
        return importlib.import_module("scripts.portfolio.export_iris")
    except ModuleNotFoundError:
        pytest.fail("The real portfolio model exporter has not been implemented.")


def test_export_matches_existing_pipeline():
    exporter = load_exporter()
    artifact = exporter.build_artifact()
    pipeline, iris, metrics = fit_example()
    scaler, classifier = pipeline.steps[0][1], pipeline.steps[1][1]
    assert artifact["schema_version"] == 1
    assert artifact["id"] == "iris-classifier"
    assert artifact["labels"] == list(iris.target_names)
    assert artifact["feature_names"] == list(iris.feature_names)
    assert artifact["classes"] == [0, 1, 2]
    for actual, expected in (
        (artifact["scaler_mean"], scaler.mean_),
        (artifact["scaler_scale"], scaler.scale_),
        (artifact["coefficients"], classifier.coef_),
        (artifact["intercepts"], classifier.intercept_),
    ):
        np.testing.assert_allclose(actual, expected, atol=5.1e-13, rtol=0)
    assert artifact["metrics"] == metrics
    assert artifact["training_split"] == {
        "train_samples": 120,
        "test_samples": 30,
        "random_state": 42,
        "stratified": True,
    }
    assert artifact["provenance"]["scikit_learn"] == "1.7.2"
    unsigned = {
        key: value for key, value in artifact.items() if key != "content_sha256"
    }
    digest = hashlib.sha256(
        json.dumps(
            unsigned, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()
    assert artifact["content_sha256"] == digest
    assert set(artifact) == {
        "schema_version",
        "id",
        "name",
        "version",
        "feature_names",
        "labels",
        "classes",
        "scaler_mean",
        "scaler_scale",
        "coefficients",
        "intercepts",
        "metrics",
        "training_split",
        "provenance",
        "content_sha256",
    }
    assert set(artifact["provenance"]) == {
        "algorithm",
        "scikit_learn",
        "model_source_sha256",
        "dataset_source",
    }


def test_export_is_deterministic():
    exporter = load_exporter()
    assert exporter.build_artifact() == exporter.build_artifact()
    assert exporter.build_oracle() == exporter.build_oracle()


def test_oracle_uses_independent_python_probabilities():
    exporter = load_exporter()
    pipeline, iris, _ = fit_example()
    cases = exporter.build_oracle()["cases"]
    assert len(cases) == 169
    np.testing.assert_equal([case["features"] for case in cases[:150]], iris.data)
    expected_rows = pipeline.predict_proba([case["features"] for case in cases])
    for case, expected in zip(cases, expected_rows, strict=True):
        assert case["label"] == iris.target_names[expected.argmax()]
        np.testing.assert_allclose(
            case["probabilities"], expected, atol=5.1e-12, rtol=0
        )


def test_check_rejects_stale_export(tmp_path, monkeypatch, capsys):
    exporter = load_exporter()
    monkeypatch.setattr(exporter, "ARTIFACT", tmp_path / "model.json")
    monkeypatch.setattr(exporter, "ORACLE", tmp_path / "oracle.json")
    assert exporter.main([]) == 0
    assert exporter.main(["--check"]) == 0
    artifact = json.loads(exporter.ARTIFACT.read_text(encoding="utf-8"))
    artifact["coefficients"][0][0] += 0.1
    exporter.ARTIFACT.write_text(json.dumps(artifact), encoding="utf-8")
    assert exporter.main(["--check"]) == 1
    assert "stale" in capsys.readouterr().err.lower()


def test_checked_in_export_is_current():
    exporter = load_exporter()
    assert exporter.main(["--check"]) == 0
