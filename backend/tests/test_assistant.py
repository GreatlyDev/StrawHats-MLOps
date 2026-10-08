import httpx
import pytest
from openai import RateLimitError

from backend.app.assistant import AssistantUnavailable, answer


@pytest.mark.parametrize(
    ("code", "required", "forbidden"),
    [
        ("insufficient_quota", "billing", "rate limit"),
        ("rate_limit_exceeded", "wait", "billing"),
        (None, "limits", "wait"),
    ],
)
def test_provider_failure_is_actionable_and_does_not_leak_details(
    monkeypatch, code, required, forbidden
):
    import langchain_openai

    class Provider:
        def __init__(self, **kwargs):
            pass

        def invoke(self, messages):
            response = httpx.Response(
                429,
                request=httpx.Request(
                    "POST", "https://api.openai.com/v1/chat/completions"
                ),
            )
            raise RateLimitError(
                "private-provider-detail", response=response, body={"code": code}
            )

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", Provider)
    with pytest.raises(AssistantUnavailable) as failure:
        answer(
            [{"role": "user", "content": "Plan resources"}],
            [],
            {},
            "test-key",
            "gpt-4.1-mini",
        )
    message = str(failure.value).lower()
    assert required in message
    assert forbidden not in message
    assert "private-provider-detail" not in message
