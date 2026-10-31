"""시연용 추가 시나리오 — 건물 전체 정전(호실이 달라도 한 건)과 같은 건물의 개별 고장(각각 따로).

기본 데모 데이터(build_plan)에는 넣지 않고, seed_demo_data.py --add-outage-demo로 그 위에 얹는다.
탐지 배치(ai detection-scan)가 건물 전체 정전 5건은 "건물 전체" 한 묶음으로, 개별 고장 3건은 묶지 않고 두는지 보여주는 용도다.
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import ReportStatus
from app.models.report import Report
from app.services.demo_data import DemoSpec

R, A = ReportStatus.RECEIVED, ReportStatus.ASSIGNED

_OUTAGE_REASON = "한 건물 여러 강의실에 동시에 영향을 주는 정전이지만 당장 위험해 보이지는 않아요."
_SINGLE_REASON = "여러 학생이 쓰는 공간의 고장이지만 당장 위험하지는 않아 보여요."


def _outage(days_ago: float, floor: str, detail: str, text: str, target: ReportStatus) -> DemoSpec:
    return DemoSpec(days_ago, "유담관", floor, detail, "전기", "P2", text, target, reason=_OUTAGE_REASON)


def _single(days_ago: float, floor: str, detail: str, text: str, priority: str = "P4") -> DemoSpec:
    return DemoSpec(
        days_ago, "유담관", floor, detail, "시설·설비", priority, text, R, reason=_SINGLE_REASON
    )


def outage_plan() -> list[DemoSpec]:
    return [
        # 건물 전체 정전: 40분 안에 서로 다른 층·호실에서 5건
        _outage(0.150, "2", "201호 강의실", "유담관 201호 강의실 전등이 다 나갔어요 건물 전체가 정전인 것 같아요", A),
        _outage(0.145, "3", "복도", "유담관 3층 복도 불이 전부 꺼졌어요 다른 층도 그런 것 같아요", A),
        _outage(0.138, "5", "502호 강의실", "유담관 502호 강의실 전기가 나갔어요 프로젝터도 안 켜져요", A),
        _outage(0.130, "1", "로비", "유담관 1층 로비도 불이 나갔어요 건물 전체가 정전이에요", R),
        _outage(0.122, "B1", "열람실", "유담관 지하 열람실 조명이 전부 꺼졌어요 건물이 다 정전된 것 같아요", R),
        # 같은 건물의 개별 고장: 층·호실이 다르고 서로 무관
        _single(0.110, "4", "410호 강의실", "유담관 410호 강의실 에어컨이 안 켜져요"),
        _single(0.100, "7", "남자 화장실", "유담관 7층 남자 화장실 변기가 막혀서 물이 안 내려가요", "P2"),
        _single(0.090, "2", "휴게실", "유담관 2층 휴게실 의자 다리가 부러졌어요"),
    ]


def outage_exists(db: Session) -> bool:
    descs = [s.description for s in outage_plan()]
    return db.scalar(select(Report.id).where(Report.description.in_(descs)).limit(1)) is not None

