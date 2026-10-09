"""ai 서비스 RAG 답변 호출(ai_client.rag_answer)과 SSE done 이벤트 형식 — DB·네트워크 없이 (1-4c)."""
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.routers.chat import _sse_once
from app.services import ai_client
from app.services.ai_client import AiServiceError, rag_answer


class FakeResponse:
    def __init__(self, data: Any, status: int = 200) -> None:
        self._data, self.status_code = data, status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=httpx.Request("POST", "http://x"), response=httpx.Response(self.status_code))

    def json(self) -> Any:
        return self._data


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai_client, "get_settings", lambda: SimpleNamespace(ai_service_url="http://ai", ai_service_secret="s"))


def test_rag_answer_parses_sources_and_masks_question(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: dict[str, Any] = {}

    def post(url: str, **kw: Any) -> FakeResponse:
        sent.update(url=url, **kw)
        return FakeResponse({"answer": "답", "sources": [{"title": "학칙 제29조", "article_no": "제29조", "url": None}]})

    monkeypatch.setattr(httpx, "post", post)
    out = rag_answer("제 학번은 20201234 휴학은요?")
    assert out.answer == "답" and out.sources[0].article_no == "제29조" and out.sources[0].url is None
    assert sent["url"] == "http://ai/api/v1/rag/answer" and sent["headers"] == {"X-Internal-Secret": "s"}
    assert "20201234" not in sent["json"]["question"]


@pytest.mark.parametrize("bad", [FakeResponse({}, 503), FakeResponse({"nope": 1})])
def test_rag_answer_failure_raises_ai_service_error(monkeypatch: pytest.MonkeyPatch, bad: FakeResponse) -> None:
    monkeypatch.setattr(httpx, "post", lambda *_a, **_k: bad)
    with pytest.raises(AiServiceError):
        rag_answer("휴학")


def test_sse_done_event_carries_sources() -> None:
    chunks = list(_sse_once("답", [{"title": "t", "article_no": None, "url": None}]))
    assert json.loads(chunks[0][6:]) == {"delta": "답"}
    assert json.loads(chunks[1][6:]) == {"done": True, "sources": [{"title": "t", "article_no": None, "url": None}]}
    assert json.loads(list(_sse_once("인사"))[1][6:]) == {"done": True, "sources": []}
