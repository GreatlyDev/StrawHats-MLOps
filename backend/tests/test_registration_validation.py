from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.main import create_app


def client_for(tmp_path: Path) -> TestClient:
    return TestClient(create_app(data_dir=tmp_path, test_mode=True))


def snapshot(client):
    return client.get("/api/models").json(), client.get("/api/events").json()


def assert_no_side_effects(client, before_models, before_events):
    assert client.get("/api/models").json() == before_models
    assert client.get("/api/events").json() == before_events


@pytest.mark.parametrize(
    "payload",
    [
        {
            "name": "   \t\n  ",
            "version": "1.0.0",
            "framework": "PyTorch",
            "image": "ghcr.io/team/model:v1",
        },
        {
            "name": "Text model",
            "version": "   \n\t ",
            "framework": "PyTorch",
            "image": "ghcr.io/team/model:v1",
        },
        {
            "name": "Text model",
            "version": "1.0.0",
            "framework": "   \n\t ",
            "image": "ghcr.io/team/model:v1",
        },
    ],
)
def test_blank_required_registration_fields_are_rejected(tmp_path, payload):
    client = client_for(tmp_path)
    before_models, before_events = snapshot(client)

    response = client.post("/api/models", json=payload)

    assert response.status_code == 422
    assert_no_side_effects(client, before_models, before_events)


def test_register_model_trims_outer_whitespace_and_persists_trimmed_values(tmp_path):
    client = client_for(tmp_path)
    payload = {
        "name": "  \tExample Model  \n",
        "version": "\n 2.0.0 \t",
        "framework": "  PyTorch  ",
        "description": "  \nA useful model\nwith notes.  ",
        "image": "ghcr.io/team/example-model:v2",
    }

    response = client.post("/api/models", json=payload)

    assert response.status_code == 201
    model = response.json()
    assert model["name"] == "Example Model"
    assert model["version"] == "2.0.0"
    assert model["framework"] == "PyTorch"
    assert model["description"] == "A useful model\nwith notes."

    stored = client.get("/api/models").json()[0]
    assert stored["name"] == "Example Model"
    assert stored["version"] == "2.0.0"
    assert stored["framework"] == "PyTorch"
    assert stored["description"] == "A useful model\nwith notes."


def test_register_model_preserves_internal_whitespace_and_accepts_empty_description(
    tmp_path,
):
    client = client_for(tmp_path)
    description = "  Capable\n\n  of  preserving\t formatting  "
    response = client.post(
        "/api/models",
        json={
            "name": "  Internal   Model Name  ",
            "version": "9.9.9",
            "framework": "  scikit-learn  ",
            "description": description,
            "image": "ghcr.io/team/internal-model:v9",
        },
    )
    assert response.status_code == 201
    assert response.json()["name"] == "Internal   Model Name"
    assert response.json()["description"] == "Capable\n\n  of  preserving\t formatting"

    response = client.post(
        "/api/models",
        json={
            "name": "Empty Description",
            "version": "9.9.10",
            "framework": "PyTorch",
            "description": " \n\t ",
            "image": "ghcr.io/team/empty-desc:v1",
        },
    )
    assert response.status_code == 201
    assert response.json()["description"] == ""


@pytest.mark.parametrize(
    "payload",
    [
        {
            "name": "Text classifier",
            "version": "2.0.0",
            "framework": "PyTorch",
            "description": "Containerized classifier",
            "image": "ghcr.io/team/model:v2",
        },
        {
            "name": "  Text classifier  ",
            "version": "\t2.0.0\n",
            "framework": "PyTorch",
            "description": "Containerized classifier",
            "image": "ghcr.io/team/model:v2",
        },
    ],
)
def test_duplicate_identity_with_padded_values_is_conflict_and_has_no_side_effects(
    tmp_path, payload
):
    client = client_for(tmp_path)
    initial = client.post(
        "/api/models",
        json={
            "name": "Text classifier",
            "version": "2.0.0",
            "framework": "PyTorch",
            "description": "Containerized classifier",
            "image": "ghcr.io/team/model:v2",
        },
    )
    assert initial.status_code == 201
    before_models, before_events = snapshot(client)

    response = client.post("/api/models", json=payload)

    assert response.status_code == 409
    assert_no_side_effects(client, before_models, before_events)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "name": "  Iris classifier  ",
            "version": "1.0.0",
            "framework": "custom",
            "description": "reserved",
            "image": "example/iris:1.0.0",
        },
        {
            "name": "Iris classifier",
            "version": "\n 1.0.0 \t",
            "framework": "custom",
            "description": "reserved",
            "image": "example/iris:1.0.0",
        },
    ],
)
def test_reserved_iris_identity_with_padded_values_returns_conflict(tmp_path, payload):
    client = client_for(tmp_path)
    before_models, before_events = snapshot(client)

    response = client.post("/api/models", json=payload)

    assert response.status_code == 409
    assert_no_side_effects(client, before_models, before_events)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "name": "A" * 80,
            "version": "B" * 40,
            "framework": "C" * 50,
            "description": "D" * 600,
            "image": "ghcr.io/team/model:v1",
        },
        {
            "name": "A" * 80,
            "version": "B" * 40,
            "framework": "C" * 50,
            "description": " \n\t ",
            "image": "ghcr.io/team/model:v1",
        },
    ],
)
def test_length_boundaries_are_valid_after_normalization(tmp_path, payload):
    client = client_for(tmp_path)

    response = client.post("/api/models", json=payload)

    assert response.status_code == 201
    assert len(response.json()["name"]) == 80
    assert len(response.json()["version"]) == 40
    assert len(response.json()["framework"]) == 50
    assert len(response.json()["description"]) in {0, 600}


@pytest.mark.parametrize(
    "payload",
    [
        {
            "name": "A" * 81,
            "version": "1.0.0",
            "framework": "PyTorch",
            "image": "ghcr.io/team/model:v1",
        },
        {
            "name": "Text",
            "version": "B" * 41,
            "framework": "PyTorch",
            "image": "ghcr.io/team/model:v1",
        },
        {
            "name": "Text",
            "version": "1.0.0",
            "framework": "C" * 51,
            "image": "ghcr.io/team/model:v1",
        },
        {
            "name": "Text",
            "version": "1.0.0",
            "framework": "PyTorch",
            "description": "D" * 601,
            "image": "ghcr.io/team/model:v1",
        },
    ],
)
def test_text_length_limits_reject_excessive_values_and_keep_state_unchanged(
    tmp_path, payload
):
    client = client_for(tmp_path)
    before_models, before_events = snapshot(client)

    response = client.post("/api/models", json=payload)

    assert response.status_code == 422
    assert_no_side_effects(client, before_models, before_events)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "name": None,
            "version": "1.0.0",
            "framework": "PyTorch",
            "image": "ghcr.io/team/model:v1",
        },
        {
            "name": "Text",
            "version": 123,
            "framework": "PyTorch",
            "image": "ghcr.io/team/model:v1",
        },
        {
            "name": ["Text"],
            "version": "1.0.0",
            "framework": "PyTorch",
            "image": "ghcr.io/team/model:v1",
        },
        {
            "name": "Text",
            "version": "1.0.0",
            "framework": {"name": "PyTorch"},
            "image": "ghcr.io/team/model:v1",
        },
    ],
)
def test_required_string_fields_reject_non_string_types_without_stringifying(
    tmp_path, payload
):
    client = client_for(tmp_path)
    before_models, before_events = snapshot(client)

    response = client.post("/api/models", json=payload)

    assert response.status_code == 422
    assert_no_side_effects(client, before_models, before_events)


def test_malformed_image_reference_is_rejected_without_side_effects(tmp_path):
    client = client_for(tmp_path)
    before_models, before_events = snapshot(client)

    response = client.post(
        "/api/models",
        json={
            "name": "Text model",
            "version": "1.0.0",
            "framework": "PyTorch",
            "description": "Container image is malformed.",
            "image": "bad image!",
        },
    )

    assert response.status_code == 422
    assert_no_side_effects(client, before_models, before_events)
