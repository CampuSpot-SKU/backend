"""신고 대화 열람 구간 선택 단위테스트 (작업 1-25, 명세서 4-2·5-1 `/admin/reports/{id}/conversation`).

DB 없이 돈다 — 세션 메시지 목록에서 "이 신고의 신고 흐름 구간"을 고르는 pick_flow와 응답 형식만 본다.
DB를 쓰는 API 테스트는 tests/test_admin_conversation_api.py (TEST_DATABASE_URL 있을 때만).
"""
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.models.enums import ChatIntent, ChatRole
from app.schemas.admin import ConversationMessage, ReportConversation
from app.services.conversation_query import CLOCK_SKEW, MAX_MESSAGES, pick_flow

T0 = datetime(2026, 10, 30, 9, 0, 0, tzinfo=UTC)
U, A = ChatRole.USER, ChatRole.ASSISTANT
REPORT, INQUIRY = ChatIntent.REPORT, ChatIntent.INQUIRY


@dataclass
class Msg:
    role: ChatRole
    content: str
    intent: ChatIntent | None
    created_at: datetime


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def flow(start: float, tag: str) -> list[Msg]:
    """신고 한 건의 대화: 학생 말 → 되묻기 → 답 → 요약 → 접수 확인 → 접수 완료 안내(intent 없음). 6개, 5초 간격."""
    return [
        Msg(U, f"{tag} 화장실 휴지가 없어요", REPORT, at(start)),
        Msg(A, f"{tag} 몇 층인가요?", REPORT, at(start + 1)),
        Msg(U, f"{tag} 3층이요", REPORT, at(start + 5)),
        Msg(A, f"{tag} 이대로 접수할까요?", REPORT, at(start + 6)),
        Msg(U, f"{tag} 네, 접수해 주세요", REPORT, at(start + 10)),
        Msg(A, f"{tag} 신고가 접수됐어요!", None, at(start + 11)),
    ]


def texts(msgs: list[Msg]) -> list[str]:
    return [m.content for m in msgs]


def test_picks_whole_report_flow_through_done_message() -> None:
    chat = [Msg(U, "안녕하세요", None, at(0)), Msg(A, "안녕하세요!", None, at(1))] + flow(20, "가")
    got, truncated = pick_flow(chat, report_created_at=at(31))
    assert texts(got) == texts(flow(20, "가")) and truncated is False


def test_excludes_inquiry_before_and_after_the_flow() -> None:
    chat = (
        [Msg(U, "휴학 언제예요?", INQUIRY, at(0)), Msg(A, "공지에 따르면…", INQUIRY, at(2))]
        + flow(20, "가")
        + [Msg(U, "장학금은요?", INQUIRY, at(40)), Msg(A, "장학과에 문의…", INQUIRY, at(42))]
    )
    got, _ = pick_flow(chat, report_created_at=at(31))
    assert texts(got) == texts(flow(20, "가"))


def test_two_reports_in_one_session_do_not_mix() -> None:
    first, second = flow(0, "가"), flow(60, "나")
    chat = first + [Msg(U, "고마워요", None, at(30)), Msg(A, "도움이 됐다니 기뻐요", None, at(31))] + second
    assert texts(pick_flow(chat, at(11))[0]) == texts(first)    # 첫 신고는 자기 구간만
    assert texts(pick_flow(chat, at(71))[0]) == texts(second)   # 둘째 신고는 앞 신고를 안 끌어옴


def test_next_report_started_right_after_done_is_not_included() -> None:
    first = flow(0, "가")
    follow = [Msg(U, "하나 더요, 복도 조명", REPORT, at(13)), Msg(A, "어느 건물인가요?", REPORT, at(14))]
    got, _ = pick_flow(first + follow, report_created_at=at(11))
    assert texts(got) == texts(first)


def test_report_time_slightly_after_done_message_still_found() -> None:
    chat = flow(0, "가")
    assert texts(pick_flow(chat, at(11) + CLOCK_SKEW - timedelta(seconds=1))[0]) == texts(chat)


def test_done_message_much_older_than_report_is_not_matched() -> None:
    chat = flow(0, "가")
    assert pick_flow(chat, at(11) + CLOCK_SKEW + timedelta(seconds=30)) == ([], False)


def test_no_done_message_returns_empty() -> None:
    chat = flow(0, "가")[:-1]  # 접수 완료 안내가 저장되지 않은 경우
    assert pick_flow(chat, at(11)) == ([], False)


def test_empty_session_returns_empty() -> None:
    assert pick_flow([], at(0)) == ([], False)


def test_long_flow_keeps_recent_messages_and_marks_truncated() -> None:
    long_flow = [Msg(U if n % 2 == 0 else A, f"말{n}", REPORT, at(n)) for n in range(MAX_MESSAGES + 20)]
    done = Msg(A, "신고가 접수됐어요!", None, at(MAX_MESSAGES + 20))
    got, truncated = pick_flow([*long_flow, done], report_created_at=at(MAX_MESSAGES + 20))
    assert truncated is True and len(got) == MAX_MESSAGES
    assert got[-1] is done and got[0].content == f"말{MAX_MESSAGES + 20 - (MAX_MESSAGES - 1)}"


def test_response_exposes_only_text_and_time() -> None:
    """익명 원칙 — 식별값·내부 판정값 필드가 응답 형식에 없어야 한다."""
    assert set(ConversationMessage.model_fields) == {"role", "content", "created_at"}
    assert set(ReportConversation.model_fields) == {"report_id", "display_no", "messages", "truncated"}
