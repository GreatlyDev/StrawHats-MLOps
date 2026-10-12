from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.app import main
from backend.app.schemas import AssistantInput, ChatMessage


def test_meaningful_markdown_is_preserved_for_both_roles():
    content = "  ## Plan\n\n- Keep **formatting** intact.\n  "

    for role in ("assistant", "user"):
        message = ChatMessage.model_validate({"role": role, "content": content})
        assert message.content == content


@pytest.mark.parametrize("content", ["", "   ", "\t", "\n", " \t\n "])
@pytest.mark.parametrize("role", ["assistant", "user"])
def test_blank_messages_return_422_without_calling_assistant(
    monkeypatch, tmp_path: Path, content, role
):
    calls = []

    def answer_spy(*args):
        calls.append(args)
        return "unexpected answer"

    monkeypatch.setattr(main, "answer", answer_spy)
    messages = [{"role": role, "content": content}]
    if role == "assistant":
        messages.append({"role": "user", "content": "Continue the plan"})

    with TestClient(main.create_app(data_dir=tmp_path, test_mode=True)) as client:
        events_before = client.get("/api/events").json()
        response = client.post("/api/assistant", json={"messages": messages})
        events_after = client.get("/api/events").json()

    assert response.status_code == 422
    if content:
        assert any(
            error["msg"] == "Value error, Message content cannot be blank."
            for error in response.json()["detail"]
        )
    assert calls == []
    assert events_after == events_before


def test_message_length_boundaries_are_preserved():
    valid = "x" * 4000
    assert (
        ChatMessage.model_validate({"role": "user", "content": valid}).content == valid
    )

    with pytest.raises(ValidationError):
        ChatMessage.model_validate({"role": "user", "content": "x" * 4001})


def test_conversation_size_role_and_final_user_boundaries_are_preserved():
    valid_messages = [{"role": "assistant", "content": "History"}] * 11
    valid_messages.append({"role": "user", "content": "Request"})
    assert (
        len(AssistantInput.model_validate({"messages": valid_messages}).messages) == 12
    )

    with pytest.raises(ValidationError):
        AssistantInput.model_validate(
            {"messages": valid_messages + [valid_messages[-1]]}
        )

    with pytest.raises(ValidationError):
        ChatMessage.model_validate({"role": "system", "content": "Not allowed"})

    with pytest.raises(ValidationError):
        AssistantInput.model_validate(
            {"messages": [{"role": "assistant", "content": "Not a final request"}]}
        )
