"""시연용 데모 데이터 시딩·초기화 (작업 S-2).

목적: 탐지(문제 후보)·예측(재발 주기)·SLA 통계·유사 사례가 시연 화면에서 자연스럽게 보이도록
"과거에 쌓인 신고 이력"을 만든다. 챗봇으로는 접수 시각을 과거로 못 바꾸므로 DB에 직접 넣는다.

- 데모 행 표시: 전용 chat_sessions 행(user_identifier = DEMO_IDENTIFIER)에 신고를 붙인다.
  초기화(reset)는 이 세션에 붙은 신고만 지운다. 실제 신고는 건드리지 않는다.
- 시각은 실행 시점(now) 기준 상대값이라, 시연 직전에 reset → apply를 다시 하면 "최근 데이터"가 된다.
- 임베딩(reports.embedding)은 비워 둔다 — 탐지 배치(ai detection-scan)가 채운다.
- (S-2b, 10/30) ① 5일 넘은 신고는 접수 시각의 시·분·초를 학생 활동 시간대로 흩뿌린다(전부 같은 초로 찍히던 것 해소).
  ② 신고마다 대화 세션 1개와 대화 기록(원문 말투 → 되묻기 → 요약 → 접수 완료 안내·AI 판정 이유)을 함께 넣어
  관리자 "신고 대화" 구역(1-25)이 채워지게 한다. ③ 운영 DB의 테스트 채팅(문의 로그 포함)을 지우는 purge_chat.
  문의 로그는 가짜로 만들지 않는다(학칙 인용이 틀리면 안 되므로 촬영 전 사이트에서 실제 질문).
- 하는 일은 scripts/seed_demo_data.py(Actions "Seed demo data")가 호출한다. 형식·시나리오는 맨 아래 build_plan.
"""

from __future__ import annotations

import random
import uuid
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, cast

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.models.admin import Admin
from app.models.chat import ChatMessage, ChatSession
from app.models.config import Building, Category, SlaConfig
from app.models.enums import ChatIntent, ChatRole, Priority, ReportStatus
from app.models.problem import PredictionStat
from app.models.report import Report, ReportStatusHistory
from app.models.user import User
from app.services import campus_places as cp
from app.services.slot_filling import (
    ASK_BUILDING_ONLY,
    ASK_LOCATION_BUILDING_WIDE,
    ASK_LOCATION_GENERIC,
    DONE_PREFIX,
)

DEMO_IDENTIFIER = "demo-seed"
KST = timezone(timedelta(hours=9))
SPREAD_FROM_DAYS = (
    5.0  # 이보다 오래된 신고만 접수 시각을 흩뿌린다(최근 신고·몰림 시나리오는 "지금 − N일" 그대로)
)
# 학생이 신고하는 시간대(KST) 가중치 — 수업 시간 사이 오전~오후에 몰리고 밤엔 드묾
HOUR_WEIGHTS = {
    8: 3,
    9: 6,
    10: 9,
    11: 10,
    12: 8,
    13: 8,
    14: 9,
    15: 9,
    16: 8,
    17: 6,
    18: 5,
    19: 3,
    20: 3,
    21: 2,
}
DEFAULT_SLA_HOURS = {"P1": 4, "P2": 24, "P3": 72, "P4": 168}

# 카테고리별 담당 부서 이름(시연용 가상 이름 — 실제 학교 부서명이 아님)
DEPT_BY_CATEGORY = {
    "전기": "시설관리팀",
    "시설·설비": "시설관리팀",
    "청소·위생": "환경위생팀",
    "안전": "안전관리팀",
    "IT·네트워크": "정보전산팀",
    "기타": "행정지원팀",
}

ASSIGN_AFTER_H = 0.5  # 접수 → 배정
START_AFTER_H = 2.0  # 접수 → 처리중
CLOSE_AFTER_RESOLVE_H = 20.0  # 해결 → 종료
MARGIN = timedelta(minutes=5)  # 미래 시각이 되지 않게 남기는 여유

DEMO_BUILDINGS = (
    "대일관",
    "문예관",
    "본관",
    "북악관",
    "상승관",
    "유담관",
    "은주1관",
    "은주2관",
    "청운관",
    "한림관",
    "혜인관",
    "수인관",
)  # 시연 데이터가 빠짐없이 쓰는 건물(12곳)

ORDER = [
    ReportStatus.RECEIVED,
    ReportStatus.ASSIGNED,
    ReportStatus.IN_PROGRESS,
    ReportStatus.RESOLVED,
    ReportStatus.CLOSED,
]


@dataclass(frozen=True)
class DemoSpec:
    days_ago: float  # 접수 시점 (실행 시각 기준 며칠 전)
    building: (
        str | None
    )  # 학교 건물 목록에서 못 찾은 위치면 None (location_raw에 학생이 말한 그대로)
    floor: str | None
    detail: str | None
    category: str
    priority: str  # "P1".."P4"
    description: str
    target: ReportStatus  # 이 단계까지 진행된 상태로 만든다(시각이 미래가 되면 앞 단계에서 멈춤)
    resolve_hours: float | None = None  # 접수 후 해결까지 걸린 시간 (target이 해결·종료일 때)
    memo: str | None = None  # 해결 처리 메모
    clock: str | None = (
        None  # 있으면 days_ago는 "N일 전(KST 날짜)", clock은 그날의 접수 시각 "HH:MM"
    )
    reason: str | None = None  # 접수 완료 안내에 쓰는 AI 판단 이유(없으면 우선순위별 기본 문장)
    location_raw: str | None = None  # 건물을 못 찾은 신고의 위치 원문


@dataclass(frozen=True)
class Step:
    status: ReportStatus
    at: datetime
    memo: str | None


R, A, P = ReportStatus.RECEIVED, ReportStatus.ASSIGNED, ReportStatus.IN_PROGRESS


def build_plan() -> list[DemoSpec]:
    """시연 시나리오(11/1 전면 재구성). 행 데이터는 demo_plan_data.PLAN_ROWS — 각 행의 분류·우선순위·판단 이유는
    실제 AI(ai 서비스 /report/judge)가 신고 문장을 보고 낸 결과 그대로다. 약 60일치 이력.

    1. 몰림 6곳(탐지 → "문제 후보"): 은주1관 3층 화장실 누수(5건·사흘), 혜인관 3층 강의실 와이파이(5건·40분),
       문예관 엘리베이터 정지(4건·45분), 유담관 지하주차장 조명(3건), 상승관 4층 강의실 난방(4건),
       한림관 식당 가스 냄새(3건·11분). 곳마다 한두 달 전 비슷한 이력이 있어 "예측"과 "유사 사례"도 자연스럽다.
    2. 반복 지점 11곳(예측 → "예방 점검"): 며칠~2주 간격으로 되풀이돼 다음 발생 시점이 앞으로 나온다.
    3. 거의 동시에 들어온 중복 접수 14쌍(다른 학생이 몇 분~1시간 안에 같은 문제를 신고), 학생 개인 단위 신고(사물함·자기 자리),
       난방 민원이 여러 건물에서 늘어나는 최근 열흘, 중간고사 주간의 IT·전기 신고 증가, 주별로 늘어나는 접수량.
    4. 해결·종료된 건(처리 메모 포함, 마감을 넘겨 해결된 건 일부)과 지금 처리 중인 건(접수·배정·처리중)이 섞여 있다.
       처리 중인 건은 모두 마감(SLA) 안.
    건물·카테고리 조합은 일부러 만든 곳만 3건 이상이고 모두 예측이 앞으로 나온다(tests/test_demo_data.py가 지킴).
    """
    from app.services.demo_plan_data import PLAN_ROWS

    status = {"R": R, "A": A, "P": P}
    plan: list[DemoSpec] = []
    for (
        day,
        clock,
        building,
        floor,
        detail,
        category,
        priority,
        description,
        hours,
        memo,
        code,
        reason,
        raw,
    ) in PLAN_ROWS:
        target = status[code] if code else ReportStatus.CLOSED
        plan.append(
            DemoSpec(
                float(day),
                building,
                floor,
                detail,
                category,
                priority,
                description,
                target,
                hours,
                memo,
                clock,
                reason,
                raw,
            )
        )
    return plan


def _rng(spec: DemoSpec, salt: str) -> random.Random:
    """신고마다 항상 같은 값이 나오는 난수 — 같은 시점에 다시 넣어도 같은 데이터, 테스트도 안정적."""
    return random.Random(
        zlib.crc32(f"{salt}|{spec.building}|{spec.floor}|{spec.description}".encode())
    )


def created_at_for(spec: DemoSpec, now: datetime) -> datetime:
    """접수 시각. clock이 있으면 "오늘(KST) − N일"의 그 시각. 없으면 now − days_ago이고, 5일 넘은 신고는 시·분·초를 학생 활동 시간대로 정한다."""
    if spec.clock:
        hh, mm = (int(x) for x in spec.clock.split(":"))
        day = (now.astimezone(KST) - timedelta(days=int(spec.days_ago))).replace(
            hour=hh, minute=mm, second=_rng(spec, "sec").randrange(60), microsecond=0
        )
        return day.astimezone(now.tzinfo or UTC)
    base = now - timedelta(days=spec.days_ago)
    if spec.days_ago < SPREAD_FROM_DAYS:
        return base
    rng = _rng(spec, "time")
    hour = rng.choices(list(HOUR_WEIGHTS), weights=list(HOUR_WEIGHTS.values()))[0]
    local = base.astimezone(KST).replace(
        hour=hour, minute=rng.randrange(60), second=rng.randrange(60), microsecond=0
    )
    return local.astimezone(base.tzinfo or UTC)


def timeline(spec: DemoSpec, now: datetime) -> list[Step]:
    """접수부터 target 단계까지의 상태 이력. 시각이 now를 넘는 단계는 만들지 않는다."""
    created = created_at_for(spec, now)
    assign_h, start_h = ASSIGN_AFTER_H, START_AFTER_H
    if spec.resolve_hours is not None:  # 빨리 해결된 건(긴급)은 배정·착수도 그 안에 끝난 것으로
        assign_h = min(assign_h, spec.resolve_hours * 0.25)
        start_h = min(start_h, spec.resolve_hours * 0.5)
    at = {
        ReportStatus.RECEIVED: created,
        ReportStatus.ASSIGNED: created + timedelta(hours=assign_h),
        ReportStatus.IN_PROGRESS: created + timedelta(hours=start_h),
    }
    if spec.resolve_hours is not None:
        resolved = created + timedelta(hours=spec.resolve_hours)
        at[ReportStatus.RESOLVED] = resolved
        at[ReportStatus.CLOSED] = resolved + timedelta(hours=CLOSE_AFTER_RESOLVE_H)
    dept = DEPT_BY_CATEGORY[spec.category]
    memos = {
        ReportStatus.RECEIVED: "챗봇 접수",
        ReportStatus.ASSIGNED: f"{dept} 배정",
        ReportStatus.IN_PROGRESS: "현장 확인 후 작업 시작",
        ReportStatus.RESOLVED: spec.memo,
        ReportStatus.CLOSED: "해결 확인 후 종료",
    }
    steps: list[Step] = []
    for status in ORDER[: ORDER.index(spec.target) + 1]:
        when = at.get(status)
        if when is None or (status is not ReportStatus.RECEIVED and when > now - MARGIN):
            break
        steps.append(Step(status, when, memos[status]))
    return steps


def final_status(spec: DemoSpec, now: datetime) -> ReportStatus:
    return timeline(spec, now)[-1].status


# ---------------------------------------------------------------- 대화 기록 (S-2b)

REASON_BY_PRIORITY = {
    "P1": [
        "다치거나 사고로 이어질 수 있는 안전 문제라 바로 확인이 필요하다고 봤어요.",
        "안전과 직접 관련된 문제라 지금 바로 확인이 필요하다고 판단했어요.",
    ],
    "P2": [
        "여러 사람이 쓰는 공간이고 불편이 커서 빨리 처리해야 한다고 봤어요.",
        "이용하는 사람이 많은 곳이고 당장 지장이 커서 서둘러 처리가 필요하다고 봤어요.",
    ],
    "P3": [
        "여러 사람이 쓰는 공간이지만 급한 위험 신호는 없다고 봤어요.",
        "이용에 불편은 있지만 안전과 관련된 신호는 없다고 판단했어요.",
    ],
    "P4": [
        "당장 위험하거나 이용에 큰 지장은 없는 문제라고 봤어요.",
        "급하지 않은 불편이라 일정에 맞춰 처리해도 된다고 봤어요.",
    ],
}
_PREFIXES = ["", "", "저기 ", "혹시 ", "안녕하세요 "]
_SUFFIXES = ["", "", "", "ㅠㅠ", " 확인 부탁드려요", "...", " 빨리 와주세요"]
_CONFIRMS = [
    "네 접수해 주세요",
    "네",
    "네 그렇게 접수해주세요",
    "넵 접수해주세요",
    "네 맞아요 접수할게요",
]


@dataclass(frozen=True)
class DemoMessage:
    role: ChatRole
    content: str
    at: datetime
    intent: ChatIntent | None
    intent_scores: dict[str, Any] | None = None


def location_text(spec: DemoSpec) -> str:
    """ "혜인관 2층 화장실"처럼 접수 완료 안내에 쓰는 위치."""
    if spec.building is None:
        return spec.location_raw or "위치 미상"
    parts = [spec.building]
    if spec.floor:
        parts.append(
            f"{spec.floor}층"
            if spec.floor.isdigit()
            else f"지하 {spec.floor[1:]}층"
            if spec.floor.startswith("B")
            else spec.floor
        )
    if spec.detail:
        parts.append(spec.detail)
    return " ".join(parts)


def build_dialogue(spec: DemoSpec, display_no: int, created: datetime) -> list[DemoMessage]:
    """접수까지의 대화. 마지막 메시지(접수 완료 안내)만 intent가 없다 — conversation_query.pick_flow가 끝으로 찾는 표시.

    같은 신고는 항상 같은 대화가 나온다. 일부는 층·건물·남/여를 한 번 되묻는 흐름이다(실제 에이전트의 되묻기와 같은 모양).
    """
    rng = _rng(spec, "dialogue")
    desc = spec.description
    first = desc
    asks: list[tuple[str, str]] = []  # (챗봇 질문, 학생 답)
    floor = spec.floor or ""
    floor_in = floor.isdigit() and f"{floor}층" in desc
    is_toilet = "화장실" in (spec.detail or "")
    roll = rng.random()
    summary = desc
    unique = cp.find_unique(desc)
    # 학생이 건물을 말하지 않았고 이름만으로 건물이 정해지는 장소도 아니면, 실제 챗봇처럼 건물을 되묻는다
    needs_building = (
        spec.building is not None
        and spec.building not in desc
        and not (unique and unique[0] == spec.building)
    )
    if needs_building:
        building = cast(str, spec.building)
        generic = spec.detail or "장소"
        if generic == "엘리베이터":
            asks.append((ASK_LOCATION_BUILDING_WIDE.format(detail=generic), f"{building}이에요"))
        elif floor_in:
            asks.append((ASK_BUILDING_ONLY, f"{building}이요"))
        elif floor.isdigit() and not unique:
            asks.append(
                (ASK_LOCATION_GENERIC.format(detail=generic), f"{building} {floor}층이에요")
            )
        else:
            asks.append((ASK_BUILDING_ONLY, f"{building}이에요"))
        summary = f"{building} {desc}"
    elif (
        floor_in and roll < 0.35 and spec.building and spec.building in desc
    ):  # 층을 빼고 말한 경우 → 층(화장실이면 남/여까지 한 번에) 되묻기
        first = " ".join(desc.replace(f"{floor}층", "", 1).split())
        if is_toilet:
            gender = rng.choice(["남자", "여자"])
            asks.append(
                (
                    f"{spec.building} 몇 층 남자/여자 화장실인가요?",
                    f"{floor}층 {gender} 화장실이에요",
                )
            )
        else:
            asks.append((f"{spec.building} 몇 층인가요?", f"{floor}층이요"))
    elif (
        spec.building and spec.building in desc and roll > 0.88
    ):  # 건물을 빼고 말한 경우 → 건물 되묻기
        first = " ".join(desc.replace(spec.building, "", 1).split())
        asks.append((ASK_BUILDING_ONLY, f"{spec.building}이에요"))
    first = rng.choice(_PREFIXES) + first + rng.choice(_SUFFIXES)

    score = rng.randint(88, 99)
    seq: list[tuple[ChatRole, str, ChatIntent | None, float, dict[str, Any] | None]] = [
        (
            ChatRole.USER,
            first,
            ChatIntent.REPORT,
            0.0,
            {"report_score": score, "inquiry_score": 100 - score},
        ),
    ]
    for question, answer in asks:
        seq.append((ChatRole.ASSISTANT, question, ChatIntent.REPORT, rng.uniform(2.5, 5.0), None))
        seq.append((ChatRole.USER, answer, ChatIntent.REPORT, rng.uniform(12, 40), None))
    seq.append(
        (
            ChatRole.ASSISTANT,
            f"“{summary}” 내용으로 정리했어요. 이대로 접수할까요?",
            ChatIntent.REPORT,
            rng.uniform(2.5, 5.0),
            None,
        )
    )
    seq.append((ChatRole.USER, rng.choice(_CONFIRMS), ChatIntent.REPORT, rng.uniform(6, 25), None))
    done = (
        f"{DONE_PREFIX}! 접수번호는 {display_no}번이에요. {location_text(spec)}에서 생긴 {spec.category} 문제로 분류했어요. "
        f"{spec.reason or rng.choice(REASON_BY_PRIORITY[spec.priority])} 그래서 {spec.priority}로 판단했어요. 담당 부서에서 확인 후 처리할게요."
    )
    seq.append((ChatRole.ASSISTANT, done, None, rng.uniform(2.5, 5.0), None))

    # 마지막(접수 완료 안내)을 접수 시각 1초 뒤에 두고 거꾸로 시각을 계산
    at = created + timedelta(seconds=1)
    out: list[DemoMessage] = []
    for role, content, intent, gap, scores in reversed(seq):
        out.append(DemoMessage(role, content, at, intent, scores))
        at -= timedelta(seconds=gap)
    out.reverse()
    return out


# ---------------------------------------------------------------- DB


@dataclass
class Counts:
    reports: int
    open: int
    resolved_or_closed: int
    sla_met: int
    sla_breached: int
    sessions: int = 0
    messages: int = 0


def count_demo(db: Session) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(Report)
            .join(ChatSession, Report.session_id == ChatSession.id)
            .where(ChatSession.user_identifier == DEMO_IDENTIFIER)
        )
        or 0
    )


def list_non_demo_reports(db: Session) -> list[tuple[int, str]]:
    """데모가 아닌 신고 — (접수번호, 설명 앞부분). 테스트 신고 일괄 삭제 대상."""
    demo_sessions = select(ChatSession.id).where(ChatSession.user_identifier == DEMO_IDENTIFIER)
    rows = db.execute(
        select(Report.display_no, Report.description)
        .where(Report.session_id.is_(None) | Report.session_id.not_in(demo_sessions))
        .order_by(Report.display_no)
    ).all()
    return [(int(n), str(d)[:30]) for n, d in rows]


def _delete_empty_clusters(db: Session) -> None:
    # 신고가 하나도 안 남은 문제 후보는 의미가 없어 지운다 (후보-신고 매핑은 신고 삭제 때 같이 사라짐)
    db.execute(
        text(
            "DELETE FROM problem_clusters WHERE id NOT IN "
            "(SELECT DISTINCT cluster_id FROM problem_cluster_reports)"
        )
    )


def reset_demo(db: Session) -> int:
    """데모 신고와 데모 세션을 지운다. 예측 통계(파생 데이터)와 빈 문제 후보도 정리. 지운 신고 수를 돌려준다."""
    demo_sessions = select(ChatSession.id).where(ChatSession.user_identifier == DEMO_IDENTIFIER)
    result = cast(
        "CursorResult[Any]", db.execute(delete(Report).where(Report.session_id.in_(demo_sessions)))
    )
    db.execute(delete(ChatSession).where(ChatSession.user_identifier == DEMO_IDENTIFIER))
    _clear_derived(db)
    return int(result.rowcount or 0)


def purge_non_demo(db: Session) -> int:
    """데모가 아닌 모든 신고(운영 DB의 테스트 신고)를 지운다. 되돌릴 수 없다."""
    demo_sessions = select(ChatSession.id).where(ChatSession.user_identifier == DEMO_IDENTIFIER)
    result = cast(
        "CursorResult[Any]",
        db.execute(
            delete(Report).where(
                Report.session_id.is_(None) | Report.session_id.not_in(demo_sessions)
            )
        ),
    )
    _clear_derived(db)
    return int(result.rowcount or 0)


def _orphan_chat_filter() -> list[Any]:
    """지워도 되는 채팅 세션: 데모가 아니고, 신고가 붙어 있지 않고, 로그인 계정에 연결되지 않은 것."""
    reported = select(Report.session_id).where(Report.session_id.is_not(None))
    return [
        ChatSession.user_identifier != DEMO_IDENTIFIER,
        ChatSession.id.not_in(reported),
        ChatSession.user_id.is_(None),
    ]


def count_orphan_chat(db: Session) -> tuple[int, int]:
    """(세션 수, 메시지 수) — purge_chat 미리보기용."""
    cond = _orphan_chat_filter()
    sessions = int(db.scalar(select(func.count()).select_from(ChatSession).where(*cond)) or 0)
    messages = int(
        db.scalar(
            select(func.count())
            .select_from(ChatMessage)
            .join(ChatSession, ChatMessage.session_id == ChatSession.id)
            .where(*cond)
        )
        or 0
    )
    return sessions, messages


def purge_orphan_chat(db: Session) -> int:
    """개발·점검 중 쌓인 테스트 채팅(문의 로그 포함)을 지운다. 되돌릴 수 없다. 지운 세션 수를 돌려준다.

    신고가 붙은 세션과 로그인 계정에 연결된 세션은 남긴다. purge_non_demo(테스트 신고 삭제) 뒤에 부르면 신고가 없어져
    그 신고의 세션도 지워진다. 지금 사이트를 쓰는 사람의 대화도 지워지니 촬영 직전 한 번만 쓸 것.
    """
    result = cast(
        "CursorResult[Any]", db.execute(delete(ChatSession).where(*_orphan_chat_filter()))
    )
    return int(result.rowcount or 0)


# 학생 테스트 계정에 연결하는 대화 8건 — 한 학생이 여러 건물에서 겪은 일처럼 상태가 골고루 섞이게 골랐다(S-2c).
# 계정을 새로 만들지는 않는다(비밀번호를 코드에 둘 수 없음) — 사이트에서 가입한 계정의 login_id를 attach_user로 넘긴다.
MINE_DESCRIPTIONS = (
    "은주1관 3층 여자 화장실 세면대 밑에서 물이 새요",
    "혜인관 301호 강의실 와이파이가 아예 안 잡혀요",
    "문예관 엘리베이터 고장났어요 계단으로 다니는 중이에요",
    "유담관 1103호 강의실 난방이 안 돼서 너무 추워요",
    "한림관 식당 주방 쪽에서 가스 냄새가 나요",
    "청운관 화장실 청소 좀 해주세요 쓰레기가 넘쳐요",
    "북악관 3층 정수기 온수가 안 나와요",
    "문예관 605호 강의실 제 자리 스탠드가 안 켜져요",
)


def find_user(db: Session, login_id: str) -> User | None:
    return db.scalar(select(User).where(User.login_id == login_id))


def attach_demo_to_user(db: Session, login_id: str) -> int:
    """데모 대화 중 MINE_DESCRIPTIONS에 해당하는 세션을 그 학생 계정에 연결한다(chat_sessions.user_id).

    연결된 수를 돌려준다. 계정이 없으면 0 — 호출한 쪽이 안내한다. 이미 다른 계정에 연결된 세션은 건드리지 않는다.
    """
    user = find_user(db, login_id)
    if user is None:
        return 0
    ids = list(
        db.scalars(
            select(Report.session_id)
            .join(ChatSession, Report.session_id == ChatSession.id)
            .where(
                ChatSession.user_identifier == DEMO_IDENTIFIER,
                ChatSession.user_id.is_(None),
                Report.description.in_(MINE_DESCRIPTIONS),
            )
        )
    )
    if not ids:
        return 0
    db.execute(update(ChatSession).where(ChatSession.id.in_(ids)).values(user_id=user.id))
    return len(ids)


def _clear_derived(db: Session) -> None:
    db.execute(delete(PredictionStat))  # 예측 배치가 통째로 다시 만드는 표
    _delete_empty_clusters(db)


def restart_numbering_if_empty(db: Session) -> bool:
    """신고가 하나도 없을 때만 접수번호를 1부터 다시 시작한다."""
    if db.scalar(select(func.count()).select_from(Report)):
        return False
    db.execute(text("SELECT setval(pg_get_serial_sequence('reports', 'display_no'), 1, false)"))
    return True


def insert_plan(db: Session, specs: Sequence[DemoSpec], now: datetime) -> Counts:
    """데모 세션을 만들고 신고·상태 이력을 넣는다 (커밋은 호출한 쪽)."""
    categories = {c.name: c.id for c in db.scalars(select(Category))}
    buildings = {b.name: b.id for b in db.scalars(select(Building))}
    sla_hours = {s.priority.value: s.sla_hours for s in db.scalars(select(SlaConfig))}
    admins = list(db.scalars(select(Admin.id).order_by(Admin.login_id)))

    reserved = set(db.scalars(select(Report.display_no)))  # 데모가 아닌 신고가 쓰는 번호는 건너뛴다
    next_no = 1
    sessions: list[ChatSession] = []
    reports: list[Report] = []
    children: list[ChatMessage | ReportStatusHistory] = []
    open_n = done_n = met = breached = message_n = 0
    for i, spec in enumerate(specs):
        if spec.category not in categories:
            raise ValueError(f"카테고리 '{spec.category}'가 없어요 (설정 > 카테고리 확인)")
        if spec.building is not None and spec.building not in buildings:
            raise ValueError(f"건물 '{spec.building}'이(가) 없어요 (설정 > 건물 확인)")
        steps = timeline(spec, now)
        created = steps[0].at
        deadline = created + timedelta(
            hours=sla_hours.get(spec.priority, DEFAULT_SLA_HOURS[spec.priority])
        )
        last = steps[-1]
        while next_no in reserved:
            next_no += 1
        display_no = next_no
        next_no += 1
        session = ChatSession(
            id=uuid.uuid4(),
            user_identifier=DEMO_IDENTIFIER,
            started_at=created - timedelta(minutes=3),
        )
        sessions.append(session)
        report = Report(
            id=uuid.uuid4(),
            display_no=display_no,
            session_id=session.id,
            category_id=categories[spec.category],
            priority=Priority(spec.priority),
            status=last.status,
            building_id=buildings[spec.building] if spec.building else None,
            location_raw=None if spec.building else spec.location_raw,
            floor=spec.floor,
            detail=spec.detail,
            description=spec.description,
            assigned_dept=DEPT_BY_CATEGORY[spec.category] if len(steps) > 1 else None,
            sla_deadline=deadline,
            escalation_level=0,
            created_at=created,
            updated_at=last.at,
        )
        reports.append(report)
        dialogue = build_dialogue(spec, display_no, created)
        session.started_at = dialogue[0].at - timedelta(seconds=20)
        for m in dialogue:
            children.append(
                ChatMessage(
                    id=uuid.uuid4(),
                    session_id=session.id,
                    role=m.role,
                    content=m.content,
                    intent=m.intent,
                    intent_scores=m.intent_scores,
                    created_at=m.at,
                )
            )
        message_n += len(dialogue)
        for j, step in enumerate(steps):
            children.append(
                ReportStatusHistory(
                    id=uuid.uuid4(),
                    report_id=report.id,
                    from_status=steps[j - 1].status if j else None,
                    to_status=step.status,
                    memo=step.memo,
                    changed_by=admins[(i + j) % len(admins)] if admins and j else None,
                    changed_at=step.at,
                )
            )
        resolved_at = next((s.at for s in steps if s.status is ReportStatus.RESOLVED), None)
        if resolved_at is None:
            open_n += 1
        else:
            done_n += 1
            if resolved_at <= deadline:
                met += 1
            else:
                breached += 1
    # 표 단위로 한 번씩 보낸다 — 원격 DB에서 신고마다 왕복하면 몇 분이 걸린다 (세션 → 신고 → 메시지·이력 순)
    for batch in (sessions, reports, children):
        db.add_all(batch)
        db.flush()
    db.execute(
        text("SELECT setval(pg_get_serial_sequence('reports', 'display_no'), :v, false)"),
        {"v": max(next_no, max(reserved, default=0) + 1)},
    )
    return Counts(
        len(specs), open_n, done_n, met, breached, sessions=len(specs), messages=message_n
    )
