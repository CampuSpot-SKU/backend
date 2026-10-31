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

DEMO_BUILDINGS = (
    "대일관", "문예관", "본관", "북악관", "상승관", "유담관",
    "은주1관", "은주2관", "청운관", "한림관", "혜인관", "수인관",
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
    """시연 시나리오(10/31 전면 재구성). 시각은 모두 "며칠 전" 상대값 — 약 60일치 이력.

    1. 몰림 5곳(탐지 → "문제 후보"): 같은 곳에서 사나흘 안에 같은 문제가 여러 학생에게서 접수된다.
       은주1관 3층 화장실 누수(5건), 혜인관 3층 강의실 와이파이(4건), 유담관 지하주차장 조명(3건),
       문예관 엘리베이터 정지(4건, 몇 시간 안), 상승관 4층 강의실 난방(3건). 각 곳마다 한두 달 전
       다른 고장 이력이 있어 "예측"과 "유사 사례"도 자연스럽다.
    2. 반복 지점 7곳(예측 → "예방 점검"): 혜인관 2층 화장실, 청운관 1층 화장실 청소, 북악관 3층 정수기,
       대일관 지하 복도 조명, 본관 8층 컨벤션홀, 상승관 4층 콘센트, 문예관 복도 청소 —
       며칠~2주 간격으로 되풀이돼 다음 발생 시점이 앞으로 나온다.
    3. 배경 신고: 12개 건물·6개 카테고리·P1~P4를 고루 쓴 해결 건(처리 메모 포함), 같은 날 중복 접수 쌍,
       마감을 넘겨 해결된 건(통계의 SLA 준수율 약 90%).
    4. 지금 처리 중인 신고는 모두 마감(SLA) 안 — 접수 직후 [긴급] 알림 메일이 한꺼번에 나가지 않게 한다.
    건물·카테고리 조합은 일부러 만든 곳만 3건 이상 — 그 밖에는 예측이 생기지 않는다(tests/test_demo_data.py가 지킴).
    """
    plan: list[DemoSpec] = []

    # ---- 1. 몰림 (문제 후보) ----
    # 은주1관 3층 화장실 누수 — 첫 신고는 임시 조치로 닫혔지만 같은 곳에서 계속 접수된다
    plan += [
        _s(47, "은주1관", "3", "화장실", "시설·설비", "P3", "은주1관 3층 화장실 수도꼭지가 헐거워서 물이 새요", 32, "수도꼭지 교체"),
        _s(26, "은주1관", "3", "화장실", "시설·설비", "P3", "은주1관 3층 화장실 변기 물이 안 내려가요", 29, "변기 부속 교체"),
        _s(2.0, "은주1관", "3", "화장실", "시설·설비", "P3", "은주1관 3층 화장실 바닥에 물이 새서 흥건해요", 9, "급수 밸브 잠가 임시 조치, 배관 점검 예정"),
        _s(1.7, "은주1관", "3", "화장실", "시설·설비", "P3", "3층 화장실 천장에서 물이 떨어져요", target=P),
        _s(1.4, "은주1관", "3", "화장실", "시설·설비", "P3", "은주1관 3층 화장실 세면대 아래에서 물이 새요", target=A),
        _s(1.0, "은주1관", "3", "화장실", "시설·설비", "P3", "3층 화장실 물이 새서 미끄러워요 다치기 전에 조치해 주세요", target=A),
        _s(0.45, "은주1관", "3", "화장실", "시설·설비", "P2", "은주1관 3층 화장실 앞 복도까지 물이 번졌어요 미끄러워서 위험해요", target=R),
    ]
    # 혜인관 3층 강의실 와이파이
    plan += [
        _s(36, "혜인관", "3", "강의실", "IT·네트워크", "P3", "혜인관 3층 강의실 와이파이가 자꾸 끊겨요", 21, "AP 재부팅, 펌웨어 업데이트"),
        _s(15, "혜인관", "3", "강의실", "IT·네트워크", "P3", "혜인관 3층 강의실 빔프로젝터 HDMI 연결이 안 돼요", 25, "HDMI 케이블 교체"),
        _s(1.6, "혜인관", "3", "강의실", "IT·네트워크", "P3", "혜인관 3층 강의실 와이파이가 끊겨서 수업 자료를 못 받아요", target=P),
        _s(1.3, "혜인관", "3", "강의실", "IT·네트워크", "P3", "3층 강의실 인터넷이 너무 느려요 접속이 자꾸 안 돼요", target=A),
        _s(0.9, "혜인관", "3", "강의실", "IT·네트워크", "P3", "혜인관 강의실 와이파이 신호가 약하고 자꾸 끊겨요", target=A),
        _s(0.4, "혜인관", "3", "강의실", "IT·네트워크", "P3", "혜인관 3층 강의실 와이파이 또 안 돼요 온라인 퀴즈 중에 끊겼어요", target=R),
    ]
    # 유담관 지하주차장 조명 (이전 이력은 같은 건물 전기 건)
    plan += [
        _s(41, "유담관", "5", "강의실", "전기", "P3", "유담관 5층 강의실 조명 일부가 꺼져요", 26, "조명 교체"),
        _s(17, "유담관", "12", "강의실", "전기", "P3", "유담관 12층 강의실 콘센트가 안 돼요", 31, "멀티탭 교체, 차단기 점검"),
        _s(2.4, "유담관", "B1", "주차장", "전기", "P3", "유담관 지하 주차장 조명이 나가서 너무 어두워요", 20, "조명 교체 — 지하 주차장 전체 점검 필요"),
        _s(1.9, "유담관", "B1", "주차장", "전기", "P3", "유담관 B1 주차장 입구 쪽 전등이 몇 개 꺼졌어요", target=A),
        _s(1.2, "유담관", "B1", "주차장", "전기", "P3", "지하주차장 조명 꺼져서 밤에 차 찾기 무서워요 확인해 주세요", target=R),
    ]
    # 문예관 엘리베이터 — 몇 시간 안에 연달아 접수 (첫 건은 재가동했지만 다시 멈춤)
    plan += [
        _s(52, "문예관", None, "엘리베이터", "시설·설비", "P3", "문예관 엘리베이터 문이 덜 닫혀서 계속 다시 열려요", 28, "도어 센서 청소·조정"),
        _s(24, "문예관", None, "엘리베이터", "시설·설비", "P3", "문예관 엘리베이터에서 층 표시가 이상하게 나와요", 33, "표시 기판 교체"),
        _s(0.30, "문예관", None, "엘리베이터", "시설·설비", "P2", "문예관 엘리베이터가 안 움직여요 버튼을 눌러도 반응이 없어요", 3, "재가동, 제어반 점검 예정"),
        _s(0.26, "문예관", None, "엘리베이터", "시설·설비", "P2", "문예관 엘리베이터 고장난 것 같아요 15층 올라가야 하는데 계단뿐이에요", target=A),
        _s(0.21, "문예관", None, "엘리베이터", "시설·설비", "P2", "엘리베이터 멈춰서 수업에 늦었어요 문예관 점검 부탁드립니다", target=A),
        _s(0.10, "문예관", None, "엘리베이터", "시설·설비", "P2", "문예관 엘리베이터 아직도 안 돼요", target=R),
    ]
    # 상승관 4층 강의실 난방
    plan += [
        _s(44, "상승관", "2", "화장실", "시설·설비", "P3", "상승관 2층 화장실 변기 물이 안 내려가요", 27, "변기 부속 교체"),
        _s(20, "상승관", "1", "복도", "시설·설비", "P3", "상승관 1층 복도 창문이 안 닫혀서 바람이 들어와요", 30, "창문 잠금 수리"),
        _s(1.8, "상승관", "4", "강의실", "시설·설비", "P3", "상승관 4층 강의실 난방이 안 돼서 너무 추워요", target=P),
        _s(1.3, "상승관", "4", "강의실", "시설·설비", "P3", "4층 강의실 히터에서 찬 바람만 나와요 수업 듣기 힘들어요", target=A),
        _s(0.7, "상승관", "4", "강의실", "시설·설비", "P3", "상승관 4층 강의실 난방 또 안 나와요 패딩 입고 수업 중이에요", target=R),
    ]

    # ---- 2. 반복 지점 (예방 점검) ----
    plan += [
        _s(54, "혜인관", "2", "화장실", "시설·설비", "P3", "혜인관 2층 화장실 변기가 막혀서 물이 안 내려가요", 26, "변기 막힘 제거, 배관 점검"),
        _s(46, "혜인관", "2", "화장실", "시설·설비", "P3", "혜인관 2층 화장실 세면대 물이 계속 새서 바닥이 젖었어요", 24, "세면대 배수 연결부 교체"),
        _s(37, "혜인관", "2", "화장실", "시설·설비", "P3", "2층 화장실 칸막이 문 잠금이 고장나서 안 잠겨요", 80, "잠금장치 교체 (부품 수급 지연)"),
        _s(29, "혜인관", "2", "화장실", "시설·설비", "P3", "혜인관 2층 화장실 변기 물이 계속 흘러요", 22, "변기 부속(플로트) 교체"),
        _s(20, "혜인관", "2", "화장실", "시설·설비", "P3", "혜인관 2층 여자 화장실 변기가 또 막혔어요", 27, "막힘 제거 — 같은 위치 반복이라 배관 상태 점검 필요"),
        _s(7, "혜인관", "2", "화장실", "시설·설비", "P3", "혜인관 2층 화장실 변기가 막혀서 물이 넘쳐요", 28, "막힘 제거 후 배관 내시경 점검 요청"),
    ]
    h2 = [
        (56, "청운관 1층 화장실 바닥이 더럽고 휴지가 없어요", 10, "바닥 청소, 비품 보충"),
        (49, "청운관 1층 화장실 냄새가 너무 심해요", 12, "배수구 청소, 방향제 비치"),
        (42, "청운관 화장실 쓰레기통이 넘쳐요", 8, "쓰레기통 비움, 수거 주기 조정 요청"),
        (35, "청운관 1층 화장실 세면대가 더럽고 비누가 없어요", 14, "세면대 청소, 비누 보충"),
        (28, "청운관 1층 화장실 휴지가 다 떨어졌어요", 9, "비품 보충"),
        (20, "청운관 화장실 청소가 안 된 것 같아요 냄새나요", 16, "청소 재실시, 점심 이후 순회 청소 추가 요청"),
        (12, "청운관 1층 여자 화장실 바닥에 물이 흥건해서 미끄러워요", 18, "물기 제거, 미끄럼 주의 표지 설치"),
        (4, "청운관 1층 화장실 쓰레기통이 또 넘치고 바닥이 지저분해요", 11, "청소 및 쓰레기통 비움 — 반복 지점이라 청소 횟수 조정 필요"),
    ]
    plan += [_s(d, "청운관", "1", "화장실", "청소·위생", "P4", desc, rh, memo) for d, desc, rh, memo in h2]
    plan += [
        _s(60, "북악관", "3", "정수기", "시설·설비", "P3", "북악관 3층 정수기에서 물이 안 나와요", 30, "급수 밸브 교체"),
        _s(46, "북악관", "3", "정수기", "시설·설비", "P3", "북악관 3층 정수기 냉수가 안 나오고 미지근해요", 27, "냉각 장치 점검, 센서 교체"),
        _s(31, "북악관", "3", "정수기", "시설·설비", "P3", "북악관 3층 정수기 물맛이 이상해요 필터 교체가 필요한 것 같아요", 20, "필터 교체"),
        _s(17, "북악관", "3", "정수기", "시설·설비", "P3", "북악관 3층 정수기 누름 버튼이 안 눌려요", 26, "버튼 패널 교체"),
        _s(6, "북악관", "3", "정수기", "시설·설비", "P3", "북악관 3층 정수기에서 물이 새서 바닥이 젖었어요", 28, "급수 호스 연결부 교체 — 노후 부품이라 순차 점검 필요"),
        _s(57, "대일관", "B1", "복도", "전기", "P3", "대일관 지하 복도 조명이 여러 개 꺼져 있어요", 30, "형광등 교체"),
        _s(39, "대일관", "B1", "복도", "전기", "P3", "대일관 지하 복도 불이 깜빡거려서 눈이 아파요", 26, "안정기 교체"),
        _s(21, "대일관", "B1", "복도", "전기", "P3", "대일관 지하 1층 복도 센서등이 안 켜져요", 34, "센서 교체"),
        _s(8, "대일관", "B1", "복도", "전기", "P3", "대일관 B1 복도 일부 구간이 어두워요 전등이 나갔어요", 28, "전구 교체, 복도 배선 점검 필요"),
        _s(50, "본관", "8", "컨벤션홀", "IT·네트워크", "P3", "본관 8층 컨벤션홀 마이크가 소리가 끊겨요", 22, "무선 마이크 수신기 교체"),
        _s(34, "본관", "8", "컨벤션홀", "IT·네트워크", "P3", "본관 컨벤션홀 프로젝터 화면이 안 나와요 행사 준비 중이에요", 18, "프로젝터 입력 케이블 교체"),
        _s(19, "본관", "8", "컨벤션홀", "IT·네트워크", "P3", "본관 8층 컨벤션홀 음향 장비에서 잡음이 심해요", 40, "믹서 접지 점검, 케이블 교체"),
        _s(5, "본관", "8", "컨벤션홀", "IT·네트워크", "P3", "컨벤션홀 와이파이가 행사 중에 계속 끊겨요", 26, "AP 채널 조정, 증설 검토 요청"),
        _s(52, "상승관", "4", "강의실", "전기", "P3", "상승관 4층 강의실 콘센트가 안 돼요", 85, "분전반 차단기 교체 (자재 대기)"),
        _s(39, "상승관", "4", "강의실", "전기", "P3", "상승관 4층 강의실 스위치를 눌러도 불이 안 켜져요", 30, "스위치 교체"),
        _s(25, "상승관", "4", "강의실", "전기", "P2", "4층 강의실 콘센트에서 스파크가 튀었어요 무서웠어요", 9, "콘센트 교체, 배선 점검"),
        _s(12, "상승관", "4", "강의실", "전기", "P3", "상승관 4층 강의실 조명 일부가 나갔어요", 33, "조명 교체"),
        _s(40, "문예관", "5", "복도", "청소·위생", "P4", "문예관 5층 복도에 쓰레기가 쌓여 있어요", 8, "쓰레기 수거"),
        _s(26, "문예관", "6", "복도", "청소·위생", "P4", "문예관 6층 복도 바닥이 지저분해요 음료 쏟은 자국이 있어요", 10, "바닥 청소"),
        _s(11, "문예관", "5", "복도", "청소·위생", "P4", "문예관 5층 복도 쓰레기통이 넘쳐요", 9, "쓰레기통 비움, 수거 횟수 조정"),
    ]

    # ---- 3. 배경 신고 (건물·카테고리마다 많아야 2건) ----
    plan += [
        # 한림관
        _s(61, "한림관", "2", "식당", "청소·위생", "P3", "한림관 2층 학생식당 바닥에 음식물이 떨어져 있어서 끈적거려요", 6, "바닥 청소, 식당 운영팀에 점심 이후 순회 청소 요청"),
        _s(33, "한림관", "1", "로비", "시설·설비", "P3", "한림관 1층 로비 자동문이 한 번에 안 열려요 몇 번 서 있어야 돼요", 85, "자동문 센서 교체 (부품 수급 지연)"),
        _s(12, "한림관", "3", "화장실", "시설·설비", "P4", "한림관 3층 화장실 핸드드라이어가 작동을 안 해요", 52, "핸드드라이어 교체"),
        _s(8, "한림관", "2", "복도", "안전", "P2", "한림관 2층 식당 앞 복도 바닥이 비 와서 너무 미끄러워요 넘어질 뻔했어요", 6, "미끄럼 방지 매트 설치, 물기 제거 주기 안내"),
        _s(2.6, "한림관", "5", "복도", "IT·네트워크", "P3", "한림관 5층 복도 와이파이가 아예 안 잡혀요", 20, "AP 전원 어댑터 교체"),
        _s(28, "한림관", "1", "로비", "기타", "P4", "한림관 1층 로비에 우산 보관함이 없어서 우산이 바닥에 널려 있어요", 60, "우산 빗물 받이·보관함 비치"),
        _s(18, "한림관", "5", "복도", "전기", "P3", "한림관 5층 복도 조명 한 줄이 나갔어요", 27, "조명 교체"),
        _s(0.25, "한림관", "2", "식당", "청소·위생", "P4", "점심 때 한림관 식당 테이블이 끈적끈적하고 닦여 있지 않았어요", target=R),
        # 은주2관
        _s(58, "은주2관", "5", "강의실", "전기", "P3", "은주2관 5층 강의실 콘센트가 안 들어와요 노트북 충전이 안 돼요", 44, "멀티탭 교체, 분전반 점검"),
        _s(37, "은주2관", "1", "카페", "청소·위생", "P4", "은주2관 1층 카페 앞 쓰레기통이 넘쳐서 바닥에 굴러다녀요", 8, "쓰레기 수거, 수거 주기 조정 요청"),
        _s(22.0, "은주2관", "5", "화장실", "시설·설비", "P3", "은주2관 5층 남자 화장실 소변기 물이 계속 내려가요", 30, "플러시 밸브 교체 (같은 날 중복 접수 2건 함께 처리)"),
        _s(21.9, "은주2관", "5", "화장실", "시설·설비", "P3", "5층 화장실 소변기에서 물 소리가 계속 나요 안 멈춰요", 30, "플러시 밸브 교체 (같은 날 중복 접수 2건 함께 처리)"),
        _s(13, "은주2관", "5", "복도", "청소·위생", "P4", "은주2관 5층 복도 정수기 아래에 물이 고여 있어요", 8, "바닥 청소, 정수기 배수 점검"),
        _s(4, "은주2관", "5", "강의실", "IT·네트워크", "P3", "은주2관 5층 강의실 빔프로젝터가 수업 중에 갑자기 꺼져요", 18, "램프 온도 센서 점검, 필터 청소"),
        _s(0.7, "은주2관", "1", "입구", "안전", "P2", "은주2관 입구 계단 난간이 헐거워서 잡으면 흔들려요", target=A),
        # 수인관
        _s(46, "수인관", "1", "체육관", "안전", "P1", "수인관 체육관 천장 마감재 일부가 떨어졌어요 위험해 보여요", 3, "구역 출입 통제 후 마감재 제거, 천장 전체 점검 완료"),
        _s(28, "수인관", "1", "체육관", "시설·설비", "P3", "수인관 체육관 샤워실 배수구가 막혀서 물이 고여요", 96, "배수구 막힘 제거 (업체 방문 일정 지연)"),
        _s(15, "수인관", "4", "실습실", "IT·네트워크", "P3", "수인관 4층 실습실 컴퓨터가 인터넷에 연결이 안 돼요", 26, "랜 케이블·스위치 포트 교체"),
        _s(10, "수인관", "4", "화장실", "청소·위생", "P4", "수인관 4층 화장실 휴지통이 가득 차서 넘쳐요", 9, "휴지통 비움, 수거 주기 조정"),
        _s(3.5, "수인관", "4", "실습실", "전기", "P3", "수인관 4층 아동학과 실습실 조명이 깜빡거려서 눈이 아파요", 36, "안정기 교체"),
        _s(6, "수인관", "1", "체육관", "기타", "P4", "수인관 체육관에 분실물 보관함이 없어서 물건 찾기 힘들어요", 90, "분실물 보관함 비치"),
        _s(1.2, "수인관", "4", "실습실", "시설·설비", "P3", "수인관 4층 실습실 창문 잠금이 풀려서 안 잠겨요", target=A),
        # 북악관
        _s(49, "북악관", "2", "컴퓨터실", "IT·네트워크", "P3", "북악관 2층 컴퓨터실 PC 두 대가 부팅이 안 돼요", 88, "PC 교체 (예비 장비 입고 대기)"),
        _s(27, "북악관", "1", "복도", "안전", "P2", "북악관 1층 강의실 앞 복도 천장 타일이 내려앉았어요", 38, "천장 타일 교체, 주변 타일 고정 점검 (자재 입고 대기)"),
        _s(18, "북악관", "B1", "학생식당", "청소·위생", "P4", "북악관 지하 학생식당 입구 바닥에 물이 고여 있어요", 6, "물기 제거, 입구 매트 교체"),
        _s(13, "북악관", "1", "강의실", "전기", "P3", "북악관 1층 강의실 형광등이 깜빡거려요", 30, "형광등·안정기 교체"),
        _s(6.5, "북악관", "9", "강의실", "IT·네트워크", "P3", "북악관 9층 강의실 마이크 소리가 안 나와요", 22, "무선 마이크 배터리·수신기 교체 (같은 날 중복 접수 함께 처리)"),
        _s(6.4, "북악관", "9", "강의실", "IT·네트워크", "P3", "9층 강의실 마이크가 안 되는데 수업이 안 돼요 확인 부탁드려요", 22, "무선 마이크 배터리·수신기 교체 (같은 날 중복 접수 함께 처리)"),
        _s(1.5, "북악관", "3", "화장실", "청소·위생", "P4", "북악관 3층 남자 화장실 휴지가 없어요", 7, "비품 보충"),
        # 대일관
        _s(53, "대일관", "3", "강의실", "IT·네트워크", "P3", "대일관 3층 강의실 프로젝터 화면이 번져서 글씨가 안 보여요", 28, "프로젝터 초점·렌즈 청소"),
        _s(30, "대일관", "6", "연습실", "시설·설비", "P2", "대일관 6층 연습실 에어컨이 안 켜져요 너무 더워요", 27, "압축기 점검 (업체 방문 일정 지연, 같은 날 중복 접수 함께 처리)"),
        _s(29.9, "대일관", "6", "연습실", "시설·설비", "P2", "대일관 6층 연습실 에어컨 고장이에요 리모컨을 눌러도 반응이 없어요", 27, "압축기 점검 (업체 방문 일정 지연, 같은 날 중복 접수 함께 처리)"),
        _s(15, "대일관", "1", "화장실", "청소·위생", "P4", "대일관 1층 여자 화장실 청소가 안 돼 있어요 냄새가 나요", 7, "청소 재실시"),
        _s(2.2, "대일관", "7", "연습실", "안전", "P2", "대일관 7층 연습실 방음문 경첩이 빠져서 문이 떨어질 것 같아요", 16, "경첩 교체, 문 전체 고정"),
        _s(0.15, "대일관", "1", "로비", "기타", "P4", "대일관 1층 로비 분실물 보관함이 잠겨 있어서 안 열려요", target=R),
        # 본관
        _s(44, "본관", "1", "자습실", "시설·설비", "P4", "본관 1층 자습실 에어컨이 너무 세서 추워요 온도 조절이 안 돼요", 25, "에어컨 설정 온도 조정, 조절기 교체"),
        _s(20, "본관", "3", "화장실", "청소·위생", "P4", "본관 3층 화장실 바닥이 젖어 있고 미끄러워요", 6, "물기 제거 및 청소"),
        _s(9, "본관", "7", "복도", "전기", "P3", "본관 7층 복도 전등이 깜빡거려요", 29, "전구 교체"),
        _s(2.8, "본관", "1", "정문", "안전", "P2", "본관 정문 앞 보도블록이 들떠서 걸려 넘어질 뻔했어요", 10, "보도블록 재시공, 임시 표시 설치"),
        _s(0.05, "본관", "3", "회의실", "안전", "P1", "본관 3층 회의실 콘센트에서 타는 냄새가 나요", target=P),
        # 문예관
        _s(35, "문예관", "3", "복도", "안전", "P2", "문예관 3층 복도 소화기 안전핀이 빠져 있어요", 15, "소화기 점검·재봉인, 전 층 소화기 확인"),
        _s(16.0, "문예관", "6", "강의실", "IT·네트워크", "P3", "문예관 6층 강의실 프로젝터 전원이 안 켜져요", 22, "전원 보드 퓨즈 교체 (같은 날 중복 접수 함께 처리)"),
        _s(15.9, "문예관", "6", "강의실", "IT·네트워크", "P3", "6층 강의실 빔프로젝터가 안 켜지는데 수업 시작해야 해요ㅠㅠ", 22, "전원 보드 퓨즈 교체 (같은 날 중복 접수 함께 처리)"),
        _s(5.5, "문예관", "2", "화장실", "전기", "P3", "문예관 2층 남자 화장실 센서등이 안 켜져요", 30, "센서 교체"),
        _s(9, "문예관", "1", "문예홀", "기타", "P4", "문예관 문예홀 입구 안내판이 떨어져 있어요", 70, "안내판 재부착"),
        # 상승관
        _s(7, "상승관", "1", "화장실", "청소·위생", "P4", "상승관 1층 화장실 청소가 안 돼 있어요", 10, "청소 재실시"),
        _s(3.0, "상승관", "3", "복도", "IT·네트워크", "P3", "상승관 3층 복도 와이파이가 약해요", 20, "AP 위치 조정"),
        # 유담관
        _s(45, "유담관", "9", "열람실", "시설·설비", "P3", "유담관 9층 열람실 냉난방 온도가 너무 높아서 답답해요", 45, "공조 설정 조정, 센서 점검"),
        _s(38, "유담관", "9", None, "기타", "P4", "유담관 9층 분리수거함이 부족해요", 100, "분리수거함 추가 배치"),
        _s(32.0, "유담관", "3", "미소식당", "청소·위생", "P3", "유담관 3층 미소식당 앞 쓰레기통이 넘쳐요", 6, "수거 및 청소 (같은 날 중복 접수 함께 처리)"),
        _s(31.9, "유담관", "3", "미소식당", "청소·위생", "P3", "유담관 미소식당 입구에 쓰레기가 쌓여서 냄새나요", 6, "수거 및 청소 (같은 날 중복 접수 함께 처리)"),
        _s(26, "유담관", "1", "주차장", "안전", "P2", "유담관 주차장 경사로 바닥이 미끄러워요 차 미끄러질 뻔했어요", 52, "임시 표지 설치, 미끄럼 방지 도료 보수 (시공 일정 조율로 지연)"),
        _s(9, "유담관", "12", "강의실", "IT·네트워크", "P3", "유담관 12층 강의실 와이파이가 느려요", 21, "AP 채널 조정"),
        _s(0.7, "유담관", "14", "강의실", "IT·네트워크", "P3", "유담관 14층 강의실 인터넷이 너무 느려요", target=R),
        # 청운관
        _s(34, "청운관", "3", "보건실", "안전", "P2", "청운관 3층 보건실 앞 복도에 물이 새서 바닥이 미끄러워요", 8, "누수 배관 임시 조치 후 바닥 건조"),
        _s(24, "청운관", "B1", "매점", "전기", "P3", "청운관 지하 매점 앞 조명이 꺼졌어요", 30, "조명 교체"),
        _s(14, "청운관", "3", "화장실", "시설·설비", "P3", "청운관 3층 화장실 칸막이 문 잠금이 고장났어요", 28, "잠금장치 교체"),
        _s(11, "청운관", "1", "복사실", "IT·네트워크", "P4", "청운관 1층 복사실 복합기가 인쇄가 안 돼요", 50, "토너 카트리지 교체"),
        _s(0.4, "청운관", "1", "복사실", "IT·네트워크", "P4", "청운관 복사실 복합기 스캔 기능이 안 돼요", target=A),
        # 혜인관
        _s(40, "혜인관", "4", "강의실", "전기", "P3", "혜인관 4층 강의실 콘센트가 안 돼요", 33, "콘센트 교체"),
        _s(32, "혜인관", "3", "계단", "안전", "P1", "혜인관 3층 계단 난간이 흔들려요 위험해요", 3.5, "난간 볼트 고정, 전 층 난간 점검 완료"),
        _s(3.2, "혜인관", "1", "로비", "전기", "P4", "혜인관 1층 로비 전등 두 개가 나갔어요", target=P),
        # 은주1관
        _s(23, "은주1관", "3", "CBE LAB", "안전", "P1", "은주1관 3층 CBE LAB 쪽에서 가스 냄새가 나요", 2, "가스 밸브 차단, 배관 연결부 교체 후 환기·누설 점검 완료"),
        _s(11, "은주1관", "5", "스튜디오", "전기", "P3", "은주1관 5층 스튜디오 조명 스위치가 고장났어요", 35, "스위치 교체"),
        _s(3.0, "은주1관", "1", "101호", "IT·네트워크", "P3", "은주1관 101호 강의실 마이크가 소리가 안 나와요", 20, "무선 마이크 수신기 교체"),
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


