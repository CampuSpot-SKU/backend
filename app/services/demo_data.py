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
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, cast

from sqlalchemy import delete, func, select, text
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.models.admin import Admin
from app.models.chat import ChatMessage, ChatSession
from app.models.config import Building, Category, SlaConfig
from app.models.enums import ChatIntent, ChatRole, Priority, ReportStatus
from app.models.problem import PredictionStat
from app.models.report import Report, ReportStatusHistory
from app.services.slot_filling import DONE_PREFIX

DEMO_IDENTIFIER = "demo-seed"
KST = timezone(timedelta(hours=9))
SPREAD_FROM_DAYS = 5.0  # 이보다 오래된 신고만 접수 시각을 흩뿌린다(최근 신고·몰림 시나리오는 "지금 − N일" 그대로)
# 학생이 신고하는 시간대(KST) 가중치 — 수업 시간 사이 오전~오후에 몰리고 밤엔 드묾
HOUR_WEIGHTS = {8: 3, 9: 6, 10: 9, 11: 10, 12: 8, 13: 8, 14: 9, 15: 9, 16: 8, 17: 6, 18: 5, 19: 3, 20: 3, 21: 2}
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
    building: str
    floor: str | None
    detail: str | None
    category: str
    priority: str  # "P1".."P4"
    description: str
    target: ReportStatus  # 이 단계까지 진행된 상태로 만든다(시각이 미래가 되면 앞 단계에서 멈춤)
    resolve_hours: float | None = None  # 접수 후 해결까지 걸린 시간 (target이 해결·종료일 때)
    memo: str | None = None  # 해결 처리 메모


@dataclass(frozen=True)
class Step:
    status: ReportStatus
    at: datetime
    memo: str | None


def _s(
    days_ago: float,
    building: str,
    floor: str | None,
    detail: str | None,
    category: str,
    priority: str,
    description: str,
    resolve_hours: float | None = None,
    memo: str | None = None,
    target: ReportStatus | None = None,
) -> DemoSpec:
    if target is None:
        target = ReportStatus.CLOSED if resolve_hours is not None else ReportStatus.RECEIVED
    return DemoSpec(
        days_ago, building, floor, detail, category, priority, description, target, resolve_hours, memo
    )


R, A, P = ReportStatus.RECEIVED, ReportStatus.ASSIGNED, ReportStatus.IN_PROGRESS


def build_plan() -> list[DemoSpec]:
    """시연 시나리오. 시각은 모두 "며칠 전" 상대값.

    1. 반복 지점(예측): 혜인관 2층 화장실 시설·설비(약 11일 간격), 청운관 1층 화장실 청소·위생(약 7일),
       북악관 3층 정수기 시설·설비(약 20일) — 신고는 한 번에 1건씩이라 탐지 후보는 안 만들고 예측만 만든다.
    2. 몰림(탐지): 은주1관 3층 화장실 누수 4건(약 22시간 안), 한림관 3층 강의실 와이파이 3건(약 18시간 안)
       — 같은 곳 72시간 안 3건 이상이라 "문제 후보"가 된다. 앞선 옛 이력도 있어 예측도 자연스럽다.
    3. 배경 신고 25건(여러 건물·카테고리, 대부분 종료) + 현재 처리 중인 신고 10건(SLA 안, 일부 임박).
    4. SLA: 해결한 건 중 5건은 마감 초과(준수율 약 89%) — 통계 화면용. 처리 중 신고는 마감 초과 없음.
    """
    plan: list[DemoSpec] = []

    # ---- 1. 반복 지점 ----
    plan += [
        _s(52, "혜인관", "2", "화장실", "시설·설비", "P3", "혜인관 2층 화장실 변기가 막혀서 물이 안 내려가요", 26, "변기 막힘 제거, 배관 점검"),
        _s(41, "혜인관", "2", "화장실", "시설·설비", "P3", "혜인관 2층 화장실 세면대 물이 계속 새서 바닥이 젖었어요", 24, "세면대 배수 연결부 교체"),
        _s(29.5, "혜인관", "2", "화장실", "시설·설비", "P3", "2층 화장실 칸막이 문 잠금이 고장나서 안 잠겨요", 80, "잠금장치 교체 (부품 수급 지연)"),
        _s(18, "혜인관", "2", "화장실", "시설·설비", "P3", "혜인관 2층 화장실 변기 물이 계속 흘러요", 22, "변기 부속(플로트) 교체"),
        _s(6.5, "혜인관", "2", "화장실", "시설·설비", "P3", "혜인관 2층 화장실 변기가 또 막혔어요 물이 넘쳐요", 28, "막힘 제거 — 같은 위치 반복이라 배관 상태 점검 필요"),
    ]
    h3 = [
        (43, "청운관 1층 화장실 바닥이 더럽고 휴지가 없어요", 10, "바닥 청소, 비품 보충"),
        (36, "청운관 1층 화장실 냄새가 심해요", 12, "배수구 청소, 방향제 비치"),
        (29, "청운관 화장실 쓰레기통이 넘쳐요", 8, "쓰레기통 비움, 수거 주기 조정 요청"),
        (22, "청운관 1층 화장실 세면대가 더럽고 비누가 없어요", 14, "세면대 청소, 비누 보충"),
        (15, "청운관 1층 화장실 휴지가 다 떨어졌어요", 9, "비품 보충"),
        (8, "청운관 화장실 청소가 안 된 것 같아요 냄새나요", 16, "청소 재실시"),
        (1.5, "청운관 1층 화장실 바닥에 물이 흥건해서 미끄러워요", 18, "물기 제거, 미끄럼 주의 표지 설치"),
    ]
    plan += [_s(d, "청운관", "1", "화장실", "청소·위생", "P4", desc, rh, memo) for d, desc, rh, memo in h3]
    plan += [
        _s(50, "북악관", "3", "정수기", "시설·설비", "P3", "북악관 3층 정수기에서 물이 안 나와요", 30, "급수 밸브 교체"),
        _s(31, "북악관", "3", "정수기", "시설·설비", "P3", "북악관 3층 정수기 냉수가 안 나오고 미지근해요", 27, "냉각 장치 점검, 센서 교체"),
        _s(11, "북악관", "3", "정수기", "시설·설비", "P3", "북악관 3층 정수기 물맛이 이상해요 필터 교체가 필요한 것 같아요", 20, "필터 교체"),
    ]

    # ---- 2. 몰림 (탐지 후보) ----
    plan += [
        _s(45, "은주1관", "3", "화장실", "시설·설비", "P3", "은주1관 3층 화장실 수도꼭지가 헐거워서 물이 새요", 32, "수도꼭지 교체"),
        _s(25, "은주1관", "3", "화장실", "시설·설비", "P3", "은주1관 3층 화장실 변기 물이 안 내려가요", 29, "변기 부속 교체"),
        _s(2.0, "은주1관", "3", "화장실", "시설·설비", "P3", "은주1관 3층 화장실 바닥에 물이 새서 흥건해요", target=P),
        _s(1.7, "은주1관", "3", "화장실", "시설·설비", "P3", "3층 화장실 천장에서 물이 떨어져요", target=P),
        _s(1.4, "은주1관", "3", "화장실", "시설·설비", "P3", "은주1관 3층 화장실 세면대 아래에서 물이 새요", target=A),
        _s(1.1, "은주1관", "3", "화장실", "시설·설비", "P3", "3층 화장실 물이 새서 미끄러워요 다치기 전에 조치해 주세요", target=R),
        _s(38, "한림관", "3", "강의실", "IT·네트워크", "P3", "한림관 3층 강의실 와이파이가 자꾸 끊겨요", 21, "AP 재부팅, 펌웨어 업데이트"),
        _s(17, "한림관", "3", "강의실", "IT·네트워크", "P3", "한림관 3층 강의실 빔프로젝터 HDMI 연결이 안 돼요", 25, "HDMI 케이블 교체"),
        _s(1.7, "한림관", "3", "강의실", "IT·네트워크", "P3", "한림관 3층 강의실 와이파이가 끊겨서 수업 자료를 못 받아요", target=P),
        _s(1.3, "한림관", "3", "강의실", "IT·네트워크", "P3", "3층 강의실 인터넷이 너무 느려요 접속이 자꾸 안 돼요", target=A),
        _s(1.0, "한림관", "3", "강의실", "IT·네트워크", "P3", "한림관 강의실 와이파이 신호가 약하고 자꾸 끊겨요", target=R),
    ]

    # ---- 3. 배경 신고(해결·종료) ----
    plan += [
        _s(55, "유담관", "3", "엘리베이터", "시설·설비", "P2", "유담관 엘리베이터가 3층에서 멈춰서 문이 안 열려요", 9, "제어 보드 점검 후 재가동"),
        _s(54, "대일관", "B1", "복도", "전기", "P3", "대일관 지하 복도 조명이 여러 개 꺼져 있어요", 30, "형광등 교체"),
        _s(53, "본관", "2", "강의실", "IT·네트워크", "P3", "본관 2층 강의실 프로젝터 화면이 안 나와요", 20, "램프 교체"),
        _s(51, "수인관", "1", "로비", "안전", "P1", "수인관 로비 바닥이 깨져서 걸려 넘어질 것 같아요", 3, "임시 안전 펜스 설치 후 보수 예약"),
        _s(49, "문예관", "2", "화장실", "청소·위생", "P4", "문예관 2층 화장실 휴지가 없어요", 12, "비품 보충"),
        _s(47, "상승관", "4", "강의실", "전기", "P3", "상승관 4층 강의실 콘센트가 안 돼요", 85, "분전반 차단기 교체 (자재 대기)"),
        _s(46, "은주2관", "2", "복도", "안전", "P2", "은주2관 2층 복도에 소화기가 보이지 않아요", 18, "소화기 재비치"),
        _s(44, "유담관", "9", None, "기타", "P4", "유담관 9층 분리수거함이 부족해요", 100, "분리수거함 추가 배치"),
        _s(42, "본관", "1", "로비", "청소·위생", "P4", "본관 로비 바닥에 음료가 쏟아져 있어요", 5, "바닥 청소"),
        _s(40, "대일관", "2", "강의실", "시설·설비", "P3", "대일관 2층 강의실 에어컨에서 물이 떨어져요", 95, "응축수 배수관 청소 (부품 수급 지연)"),
        _s(37, "한림관", "1", "화장실", "시설·설비", "P3", "한림관 1층 화장실 문 손잡이가 빠졌어요", 50, "손잡이 교체"),
        _s(35, "청운관", "3", "강의실", "IT·네트워크", "P3", "청운관 3층 강의실 마이크가 안 나와요", 28, "무선 마이크 수신기 교체"),
        _s(33, "북악관", "1", "로비", "전기", "P2", "북악관 1층 로비 전등이 깜빡거려요", 20, "안정기 교체"),
        _s(32, "혜인관", "3", "계단", "안전", "P1", "혜인관 3층 계단 난간이 흔들려요 위험해요", 3.5, "난간 볼트 고정, 전 층 난간 점검 완료"),
        _s(30, "수인관", "3", "강의실", "시설·설비", "P4", "수인관 3층 강의실 의자 다리가 부러졌어요", 60, "의자 교체"),
        _s(27, "상승관", "1", "화장실", "청소·위생", "P4", "상승관 1층 화장실 청소가 안 돼 있어요", 10, "청소 재실시"),
        _s(24, "문예관", "3", "강의실", "IT·네트워크", "P3", "문예관 3층 강의실 컴퓨터가 부팅이 안 돼요", 80, "SSD 교체 (재고 대기)"),
        _s(21, "대일관", "B2", "복도", "전기", "P2", "대일관 지하2층 복도 일부가 정전이에요", 7, "차단기 복구, 누전 원인 점검"),
        _s(19, "은주2관", "4", "강의실", "전기", "P3", "은주2관 4층 강의실 조명 스위치가 고장났어요", 35, "스위치 교체"),
        _s(14, "본관", "3", None, "기타", "P4", "본관 3층 게시판 고정이 풀렸어요", 70, "게시판 재고정"),
        _s(12, "한림관", "2", "복도", "시설·설비", "P2", "한림관 2층 복도 창문이 안 닫혀서 바람이 들어와요", 30, "창문 레일 수리 (작업 지연)"),
        _s(9, "문예관", "2", "복도", "청소·위생", "P4", "문예관 2층 복도에 쓰레기가 쌓여 있어요", 8, "쓰레기 수거"),
        _s(7, "수인관", "2", "엘리베이터", "안전", "P1", "수인관 엘리베이터에서 이상한 소리가 나요", 2.5, "점검 업체 출동, 베어링 교체 예약"),
        _s(4, "대일관", "1", "로비", "IT·네트워크", "P3", "대일관 1층 로비 와이파이가 안 잡혀요", 22, "AP 재설정"),
        _s(3, "유담관", "5", "강의실", "전기", "P3", "유담관 5층 강의실 조명 일부가 꺼져요", 26, "조명 교체"),
    ]

    # ---- 4. 현재 처리 중 ----
    plan += [
        _s(0.03, "유담관", "3", "화장실", "청소·위생", "P4", "유담관 3층 화장실에 비누가 없어요", target=R),
        _s(0.05, "본관", "2", "강의실", "안전", "P1", "본관 2층 강의실 콘센트에서 타는 냄새가 나요", target=R),
        _s(0.4, "상승관", "2", "화장실", "시설·설비", "P2", "상승관 2층 화장실 변기 물이 안 내려가요", target=A),
        _s(0.3, "문예관", "1", "로비", "IT·네트워크", "P3", "문예관 1층 로비 와이파이가 느려요", target=A),
        _s(0.58, "수인관", "1", "복도", "전기", "P2", "수인관 1층 복도 조명이 나가서 어두워요", target=P),
        _s(0.8, "청운관", "3", "강의실", "시설·설비", "P3", "청운관 3층 강의실 블라인드가 고장났어요", target=P),
        _s(2.3, "대일관", "B1", "화장실", "청소·위생", "P4", "대일관 지하 화장실 청소 상태가 안 좋아요", target=P),
        _s(3.2, "혜인관", "1", "로비", "전기", "P4", "혜인관 1층 로비 전등 두 개가 나갔어요", target=P),
        _s(1.0, "은주2관", "1", "로비", "기타", "P4", "은주2관 1층 로비 안내 표지판이 떨어졌어요", target=A),
        _s(0.6, "한림관", "2", "복도", "청소·위생", "P4", "한림관 2층 복도 바닥이 지저분해요", target=R),
    ]
    return plan


def _rng(spec: DemoSpec, salt: str) -> random.Random:
    """신고마다 항상 같은 값이 나오는 난수 — 같은 시점에 다시 넣어도 같은 데이터, 테스트도 안정적."""
    return random.Random(zlib.crc32(f"{salt}|{spec.building}|{spec.floor}|{spec.description}".encode()))


def created_at_for(spec: DemoSpec, now: datetime) -> datetime:
    """접수 시각. 5일 넘은 신고는 그날(KST)의 학생 활동 시간대로 시·분·초를 정한다."""
    base = now - timedelta(days=spec.days_ago)
    if spec.days_ago < SPREAD_FROM_DAYS:
        return base
    rng = _rng(spec, "time")
    hour = rng.choices(list(HOUR_WEIGHTS), weights=list(HOUR_WEIGHTS.values()))[0]
    local = base.astimezone(KST).replace(hour=hour, minute=rng.randrange(60), second=rng.randrange(60), microsecond=0)
    return local.astimezone(base.tzinfo or UTC)


def timeline(spec: DemoSpec, now: datetime) -> list[Step]:
    """접수부터 target 단계까지의 상태 이력. 시각이 now를 넘는 단계는 만들지 않는다."""
    created = created_at_for(spec, now)
    at = {
        ReportStatus.RECEIVED: created,
        ReportStatus.ASSIGNED: created + timedelta(hours=ASSIGN_AFTER_H),
        ReportStatus.IN_PROGRESS: created + timedelta(hours=START_AFTER_H),
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
_CONFIRMS = ["네 접수해 주세요", "네", "네 그렇게 접수해주세요", "넵 접수해주세요", "네 맞아요 접수할게요"]


@dataclass(frozen=True)
class DemoMessage:
    role: ChatRole
    content: str
    at: datetime
    intent: ChatIntent | None
    intent_scores: dict[str, Any] | None = None


def location_text(spec: DemoSpec) -> str:
    """"혜인관 2층 화장실"처럼 접수 완료 안내에 쓰는 위치."""
    parts = [spec.building]
    if spec.floor:
        parts.append(f"{spec.floor}층" if spec.floor.isdigit() else f"지하{spec.floor[1:]}층" if spec.floor.startswith("B") else spec.floor)
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
    is_toilet = spec.detail == "화장실"
    roll = rng.random()
    if floor_in and roll < 0.35:  # 층을 빼고 말한 경우 → 층(화장실이면 남/여까지 한 번에) 되묻기
        first = " ".join(desc.replace(f"{floor}층", "", 1).split())
        if is_toilet:
            gender = rng.choice(["남자", "여자"])
            asks.append((f"{spec.building} 몇 층 남자/여자 화장실인가요?", f"{floor}층 {gender} 화장실이에요"))
        else:
            asks.append((f"{spec.building} 몇 층인가요?", f"{floor}층이요"))
    elif spec.building in desc and roll > 0.88:  # 건물을 빼고 말한 경우 → 건물 되묻기
        first = " ".join(desc.replace(spec.building, "", 1).split())
        asks.append(("어느 건물인가요?", f"{spec.building}이에요"))
    first = rng.choice(_PREFIXES) + first + rng.choice(_SUFFIXES)

    score = rng.randint(88, 99)
    seq: list[tuple[ChatRole, str, ChatIntent | None, float, dict[str, Any] | None]] = [
        (ChatRole.USER, first, ChatIntent.REPORT, 0.0, {"report_score": score, "inquiry_score": 100 - score}),
    ]
    for question, answer in asks:
        seq.append((ChatRole.ASSISTANT, question, ChatIntent.REPORT, rng.uniform(2.5, 5.0), None))
        seq.append((ChatRole.USER, answer, ChatIntent.REPORT, rng.uniform(12, 40), None))
    seq.append(
        (ChatRole.ASSISTANT, f"“{desc}” 내용으로 정리했어요. 이대로 접수할까요?", ChatIntent.REPORT, rng.uniform(2.5, 5.0), None)
    )
    seq.append((ChatRole.USER, rng.choice(_CONFIRMS), ChatIntent.REPORT, rng.uniform(6, 25), None))
    done = (
        f"{DONE_PREFIX}! 접수번호는 {display_no}번이에요. {location_text(spec)}에서 생긴 {spec.category} 문제로 분류했어요. "
        f"{rng.choice(REASON_BY_PRIORITY[spec.priority])} 그래서 {spec.priority}로 판단했어요. 담당 부서에서 확인 후 처리할게요."
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
    result = cast("CursorResult[Any]", db.execute(delete(Report).where(Report.session_id.in_(demo_sessions))))
    db.execute(delete(ChatSession).where(ChatSession.user_identifier == DEMO_IDENTIFIER))
    _clear_derived(db)
    return int(result.rowcount or 0)


def purge_non_demo(db: Session) -> int:
    """데모가 아닌 모든 신고(운영 DB의 테스트 신고)를 지운다. 되돌릴 수 없다."""
    demo_sessions = select(ChatSession.id).where(ChatSession.user_identifier == DEMO_IDENTIFIER)
    result = cast(
        "CursorResult[Any]",
        db.execute(
            delete(Report).where(Report.session_id.is_(None) | Report.session_id.not_in(demo_sessions))
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
    result = cast("CursorResult[Any]", db.execute(delete(ChatSession).where(*_orphan_chat_filter())))
    return int(result.rowcount or 0)


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

    open_n = done_n = met = breached = message_n = 0
    for i, spec in enumerate(specs):
        if spec.category not in categories:
            raise ValueError(f"카테고리 '{spec.category}'가 없어요 (설정 > 카테고리 확인)")
        if spec.building not in buildings:
            raise ValueError(f"건물 '{spec.building}'이(가) 없어요 (설정 > 건물 확인)")
        steps = timeline(spec, now)
        created = steps[0].at
        deadline = created + timedelta(hours=sla_hours.get(spec.priority, DEFAULT_SLA_HOURS[spec.priority]))
        last = steps[-1]
        session = ChatSession(user_identifier=DEMO_IDENTIFIER, started_at=created - timedelta(minutes=3))
        db.add(session)
        db.flush()
        report = Report(
            session_id=session.id,
            category_id=categories[spec.category],
            priority=Priority(spec.priority),
            status=last.status,
            building_id=buildings[spec.building],
            floor=spec.floor,
            detail=spec.detail,
            description=spec.description,
            assigned_dept=DEPT_BY_CATEGORY[spec.category] if len(steps) > 1 else None,
            sla_deadline=deadline,
            escalation_level=0,
            created_at=created,
            updated_at=last.at,
        )
        db.add(report)
        db.flush()  # display_no가 정해져야 접수 완료 안내 문구에 번호를 쓸 수 있음
        dialogue = build_dialogue(spec, report.display_no, created)
        session.started_at = dialogue[0].at - timedelta(seconds=20)
        for m in dialogue:
            db.add(
                ChatMessage(
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
            db.add(
                ReportStatusHistory(
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
    return Counts(len(specs), open_n, done_n, met, breached, sessions=len(specs), messages=message_n)


