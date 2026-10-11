from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from openai import (
    APIConnectionError,
    APIStatusError,
    AuthenticationError,
    RateLimitError,
)

from backend.app import main
from backend.app.assistant import AssistantUnavailable, answer

SAFE_EMPTY_RESPONSE = (
    "The AI provider returned an empty or unsupported response. Please retry."
)


def patch_provider(monkeypatch, *, content=None, error=None):
    import langchain_openai

    provider_options = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            provider_options.update(kwargs)

        def invoke(self, messages):
            if error is not None:
                raise error
            return SimpleNamespace(content=content)

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", FakeChatOpenAI)
    return provider_options


def call_answer(monkeypatch, content):
    patch_provider(monkeypatch, content=content)
    return answer([], [], {}, "dummy-test-key", "test-model")


def test_plain_text_is_returned_unchanged(monkeypatch):
    content = "## Plan\n\n- Keep **formatting** intact.\n"

    assert call_answer(monkeypatch, content) == content


@pytest.mark.parametrize("content", ["", " \t\n "])
def test_blank_string_raises_safe_error(monkeypatch, content):
    with pytest.raises(AssistantUnavailable, match=f"^{SAFE_EMPTY_RESPONSE}$"):
        call_answer(monkeypatch, content)


def test_list_joins_supported_text_segments_in_order(monkeypatch):
    content = [
        "First block",
        {"type": "text", "text": "**Second block**\nwith formatting"},
        {"text": "Third block"},
        {"type": "output_text", "text": "Fourth block"},
    ]

    assert call_answer(monkeypatch, content) == (
        "First block\n**Second block**\nwith formatting\nThird block\nFourth block"
    )


def test_mixed_malformed_and_unsupported_blocks_keep_only_text(monkeypatch):
    content = [
        {"type": "image", "text": "not answer text"},
        42,
        {"type": "tool_call", "text": "private tool data"},
        {"type": "unknown", "text": "unknown type"},
        {"type": "text", "text": 10},
        {"type": "text", "text": ["not", "text"]},
        {"type": "text", "text": {"private": "value"}},
        {"type": "text", "text": "  "},
        "Useful answer",
    ]

    assert call_answer(monkeypatch, content) == "Useful answer"


@pytest.mark.parametrize(
    "content",
    [
        [],
        ["", " \t", None, 42, {"type": "image", "url": "private"}],
        None,
        42,
        {"type": "text", "text": "unsupported top-level dictionary"},
    ],
)
def test_unsupported_or_empty_content_raises_safe_error(monkeypatch, content):
    with pytest.raises(AssistantUnavailable, match=f"^{SAFE_EMPTY_RESPONSE}$"):
        call_answer(monkeypatch, content)


def test_provider_settings_remain_unchanged(monkeypatch):
    options = patch_provider(monkeypatch, content="answer")

    assert answer([], [], {}, "dummy-test-key", "test-model") == "answer"
    assert options == {
        "model": "test-model",
        "api_key": "dummy-test-key",
        "temperature": 0.2,
        "max_tokens": 600,
        "timeout": 30,
        "max_retries": 0,
    }


def provider_error(kind):
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    if kind == "authentication":
        response = httpx.Response(401, request=request)
        return AuthenticationError(
            "private-provider-detail", response=response, body={}
        )
    if kind == "connection":
        return APIConnectionError(request=request)
    if kind == "status":
        response = httpx.Response(500, request=request)
        return APIStatusError("private-provider-detail", response=response, body={})
    response = httpx.Response(429, request=request)
    return RateLimitError(
        "private-provider-detail",
        response=response,
        body={"code": "rate_limit_exceeded"},
    )


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("authentication", "did not accept"),
        ("connection", "could not be reached"),
        ("status", "returned an error"),
        ("rate_limit", "wait briefly"),
    ],
)
def test_provider_errors_remain_actionable_and_sanitized(monkeypatch, kind, expected):
    patch_provider(monkeypatch, error=provider_error(kind))

    with pytest.raises(AssistantUnavailable) as failure:
        answer([], [], {}, "dummy-test-key", "test-model")

    assert expected in str(failure.value).lower()
    assert "private-provider-detail" not in str(failure.value)


def test_missing_key_error_remains_actionable():
    with pytest.raises(AssistantUnavailable, match="not configured"):
        answer([], [], {}, None, "test-model")


@pytest.mark.parametrize(
    ("content", "status_code"),
    [(None, 503), ("Formatted **answer**", 200)],
)
def test_api_assistant_preserves_error_and_success_contract(
    monkeypatch, tmp_path, content, status_code
):
    from backend.app.assistant import answer as actual_answer

    patch_provider(monkeypatch, content=content)

    def answer_with_dummy_key(messages, models, overview, key, model_name):
        return actual_answer(messages, models, overview, "dummy-test-key", model_name)

    monkeypatch.setattr(main, "answer", answer_with_dummy_key)
    with TestClient(main.create_app(data_dir=tmp_path, test_mode=True)) as client:
        response = client.post(
            "/api/assistant",
            json={"messages": [{"role": "user", "content": "Plan resources"}]},
        )

    assert response.status_code == status_code
    if status_code == 503:
        assert response.json() == {"detail": SAFE_EMPTY_RESPONSE}
    else:
        assert response.json()["message"] == content
        assert set(response.json()) == {"message", "model"}
