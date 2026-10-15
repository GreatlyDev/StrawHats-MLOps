from pathlib import Path

import pytest

from backend.app.store import Store


def _store(directory: Path) -> Store:
    return Store(directory)


def test_summary_starts_empty(tmp_path):
    store = _store(tmp_path)

    assert store.prediction_summary("missing-model") == {
        "model_id": "missing-model",
        "predictions": 0,
        "label_counts": {},
        "median_latency_ms": None,
        "recent_inferences": [],
    }


def test_summary_filters_interleaved_models_and_sorts_labels(tmp_path):
    store = _store(tmp_path)
    store.prediction("model-a", "zebra", 3.0)
    store.prediction("model-b", "ignored", 80.0)
    store.prediction("model-a", "apple", 5.0)
    store.prediction("model-a", "zebra", 7.0)
    store.prediction("model-b", "ignored", 90.0)

    summary = store.prediction_summary("model-a")

    assert summary["predictions"] == 3
    assert summary["label_counts"] == {"apple": 1, "zebra": 2}
    assert list(summary["label_counts"]) == ["apple", "zebra"]
    assert [row["label"] for row in summary["recent_inferences"]] == [
        "zebra",
        "apple",
        "zebra",
    ]
    assert summary["median_latency_ms"] == 5.0
    assert all(row["model_id"] == "model-a" for row in summary["recent_inferences"])
    assert all(
        set(row) == {"model_id", "label", "latency_ms", "created_at"}
        for row in summary["recent_inferences"]
    )


def test_summary_counts_all_rows_but_windows_latest_30_per_model(tmp_path):
    store = _store(tmp_path)
    for index in range(35):
        store.prediction("target", f"label-{index % 3}", float(index + 1))
    for index in range(4):
        store.prediction("other", "other-label", 1000.0 + index)

    summary = store.prediction_summary("target")

    assert summary["predictions"] == 35
    assert summary["label_counts"] == {"label-0": 12, "label-1": 12, "label-2": 11}
    assert [row["latency_ms"] for row in summary["recent_inferences"]] == list(
        map(float, range(6, 36))
    )
    assert summary["median_latency_ms"] == 20.5


@pytest.mark.parametrize(
    ("latencies", "expected_median"),
    [([9.0], 9.0), ([2.0, 8.0], 5.0), ([1.0, 5.0, 9.0], 5.0)],
)
def test_summary_median_handles_odd_and_even_windows(
    tmp_path, latencies, expected_median
):
    store = _store(tmp_path)
    for latency in latencies:
        store.prediction("model-a", "label", latency)

    assert store.prediction_summary("model-a")["median_latency_ms"] == expected_median


def test_summary_orders_equal_timestamps_by_insertion_id(tmp_path):
    store = _store(tmp_path)
    for label in ("first", "second", "third"):
        store.prediction("model-a", label, 1.0)
    with store.connect() as db:
        db.execute(
            "UPDATE inferences SET created_at=? WHERE model_id=?",
            ("same timestamp", "model-a"),
        )

    summary = store.prediction_summary("model-a")

    assert [row["label"] for row in summary["recent_inferences"]] == [
        "first",
        "second",
        "third",
    ]
    assert {row["created_at"] for row in summary["recent_inferences"]} == {
        "same timestamp"
    }


def test_summary_treats_sql_looking_model_id_as_data(tmp_path):
    store = _store(tmp_path)
    store.prediction("real-model", "protected", 12.0)
    unusual_id = "missing' OR 1=1 --"

    assert store.prediction_summary(unusual_id) == {
        "model_id": unusual_id,
        "predictions": 0,
        "label_counts": {},
        "median_latency_ms": None,
        "recent_inferences": [],
    }
    assert store.prediction_summary("real-model")["predictions"] == 1


def test_summary_reads_are_read_only_and_survive_reopen(tmp_path):
    store = _store(tmp_path)
    store.prediction("model-a", "setosa", 4.0)
    store.prediction("model-a", "versicolor", 6.0)
    overview_before = store.overview()
    events_before = store.events()

    expected = store.prediction_summary("model-a")
    assert store.prediction_summary("model-a") == expected
    assert store.overview() == overview_before
    assert store.events() == events_before

    reopened = Store(tmp_path)
    assert reopened.prediction_summary("model-a") == expected
    assert reopened.overview() == overview_before
    assert reopened.events() == events_before
