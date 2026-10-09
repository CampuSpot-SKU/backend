"""학번·전화번호 마스킹 (작업 1-23) — DB 없이 도는 테스트 (CI).

패턴 표(명세 11장, 지시서 briefs/1-23.md 7장)와 ai_client가 Gemini 쪽으로 실제로 보내는 JSON을 확인한다.
예시 번호·학번은 전부 가짜 값.
"""
import json
from collections.abc import Iterator
from typing import Any

import pytest

from app.config import get_settings
from app.services import ai_client
from app.services.ai_client import AgentState, HistoryItem
from app.services.pii_mask import MASK, mask_pii

MASKED = [
    ("010-1234-5678", "***"),
    ("01012345678", "***"),
    ("010 1234 5678", "***"),
    ("010.1234.5678", "***"),
    ("011-234-5678", "***"),
    ("+82 10-1234-5678", "***"),
    ("+82-10-1234-5678", "***"),
    ("02-940-7114", "***"),
    ("031-123-4567", "***"),
    ("제 학번은 20201234예요", "제 학번은 ***예요"),
    ("학번 2020123", "학번 ***"),
    ("학번: 202012345", "학번: ***"),
    ("2020123456", "***"),
    ("20201234", "***"),
    ("학번 20201234 연락처 010-1234-5678 화장실 물이 새요", "학번 *** 연락처 *** 화장실 물이 새요"),
]

UNCHANGED = [
    "3동 2층 화장실 물이 계속 새요",
    "공학관 301호 프로젝터가 안 켜져요",
    "혜인관 1204호 콘센트가 나가요",
    "접수번호 12번 처리됐나요?",
    "2026년 9월 30일에 처음 봤어요",
    "14:30쯤 확인했어요",
    "B1 주차장 조명이 나가요",
    "5층 복도",
    "학번 확인은 어떻게 하나요?",
    "12345",
    "1234567",
    "",
]


@pytest.mark.parametrize(("text", "expected"), MASKED)
def test_masked(text: str, expected: str) -> None:
    assert mask_pii(text) == expected


@pytest.mark.parametrize("text", UNCHANGED)
def test_unchanged(text: str) -> None:
    assert mask_pii(text) == text


@pytest.mark.parametrize(("text", "expected"), MASKED)
def test_idempotent(text: str, expected: str) -> None:
    assert mask_pii(mask_pii(text)) == mask_pii(text)


def test_each_value_masked_separately() -> None:
    assert mask_pii("학번 20201234 / 010-1234-5678").count(MASK) == 2


def test_known_limit_compact_date_is_masked() -> None:
    """알려진 한계(기록 3장): 붙여 쓴 날짜도 8자리 숫자라 가려짐 — 과잉 마스킹이 안전한 쪽."""
    assert mask_pii("20260930에 봤어요") == "***에 봤어요"


# ---------- Gemini 경계: ai_client가 ai 서비스로 보내는 JSON ----------

PHONE, STUDENT = "010-1234-5678", "20201234"


class _Res:
    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._body


class Sent:
    """httpx.post 대신 보낸 payload를 모으고 정해 둔 응답을 돌려줌."""

    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []
        self.reply: dict[str, Any] = {}

    def post(self, url: str, json: dict[str, Any], **_kw: Any) -> _Res:
        self.payloads.append(json)
        return _Res(self.reply)


@pytest.fixture()
def sent(monkeypatch: pytest.MonkeyPatch) -> Iterator[Sent]:
    monkeypatch.setenv("AI_SERVICE_URL", "http://ai.test")
    monkeypatch.setenv("AI_SERVICE_SECRET", "unit-test")
    get_settings.cache_clear()
    fake = Sent()
    monkeypatch.setattr(ai_client.httpx, "post", fake.post)
    yield fake
    get_settings.cache_clear()


def _assert_clean(payload: dict[str, Any]) -> None:
    dumped = json.dumps(payload, ensure_ascii=False)
    assert PHONE not in dumped and "01012345678" not in dumped
    assert STUDENT not in dumped
    assert MASK in dumped


def test_classify_intent_sends_masked(sent: Sent) -> None:
    sent.reply = {"intent": "report", "report_score": 90, "inquiry_score": 10}
    ai_client.classify_intent(
        f"제 번호는 {PHONE}이에요", [HistoryItem(role="user", content=f"학번 {STUDENT}")]
    )
    _assert_clean(sent.payloads[0])


def test_say_text_sends_masked_and_keeps_check_consistent(sent: Sent) -> None:
    sent.reply = {"text": "위치를 알려주시겠어요? ***"}
    out = ai_client.say_text(
        "ask", f"위치를 알려주세요. {PHONE}", ["위치", PHONE],
        [HistoryItem(role="user", content=f"학번은 {STUDENT}이고 물이 새요")],
    )
    _assert_clean(sent.payloads[0])
    # must_include도 같은 기준으로 마스킹 → 다듬은 문장이 원본 번호를 안 담아도 채택됨
    assert out == "위치를 알려주시겠어요? ***"


def test_judge_report_sends_masked(sent: Sent) -> None:
    sent.reply = {"category": "시설·설비", "impact": "high", "urgency": "low",
                  "problem_stated": True, "reason": "누수"}
    ai_client.judge_report(f"물이 새요 {PHONE}", f"3동 2층 학번 {STUDENT}", ["시설·설비"])
    _assert_clean(sent.payloads[0])


def test_report_turn_sends_masked(sent: Sent) -> None:
    """1-3f 이후 생긴 에이전트 호출도 Gemini 경계 — 대화·상태·힌트 모두 마스킹."""
    sent.reply = {"action": "ask", "message": "어디인가요?", "choices": [],
                  "state": AgentState().model_dump()}
    state = AgentState(problem=f"물이 새요 {PHONE}", location_note=f"학번 {STUDENT}",
                       pending_issues=[f"연락처 {PHONE}"])
    ai_client.report_turn(
        [HistoryItem(role="user", content=f"제 학번은 {STUDENT}이고 3동 2층 화장실 물이 새요")],
        state, "ask", 1, [{"name": "북악관"}], [], hints=[f"전화 {PHONE}"],
    )
    payload = sent.payloads[0]
    _assert_clean(payload)
    assert payload["buildings"] == [{"name": "북악관"}]  # 학교 데이터는 그대로
    assert "3동 2층 화장실" in payload["conversation"][0]["content"]  # 위치는 안 지워짐


def test_judge_report_with_photo_sends_base64(sent: Sent) -> None:
    sent.reply = {"category": "시설·설비", "impact": "high", "urgency": "low",
                  "problem_stated": True, "reason": "누수", "photo_note": "천장 얼룩"}
    res = ai_client.judge_report("물이 새요", "3동 2층", ["시설·설비"], (b"\xff\xd8\xff", "image/jpeg"))
    assert res.photo_note == "천장 얼룩"
    assert sent.payloads[0]["photo_mime"] == "image/jpeg"
    assert sent.payloads[0]["photo_base64"] == "/9j/"
    ai_client.judge_report("물이 새요", "3동 2층", ["시설·설비"])
    assert "photo_base64" not in sent.payloads[1]
