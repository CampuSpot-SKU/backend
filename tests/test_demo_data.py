"""데모 데이터 시나리오 검사 — DB 없이 도는 테스트 (작업 S-2).

시연 화면이 의도대로 나오는지(탐지 후보 2곳, 예측 날짜가 미래, SLA 준수율, 처리 중 신고는 마감 안)를
계획 데이터만으로 확인한다. 실제 삽입은 임시 DB로 따로 돌려 본다.
"""
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from itertools import pairwise

from app.models.enums import ReportStatus
from app.services import demo_data
from app.services.demo_data import DEFAULT_SLA_HOURS, DemoSpec

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
OPEN = {ReportStatus.RECEIVED, ReportStatus.ASSIGNED, ReportStatus.IN_PROGRESS}


def _created(spec: DemoSpec) -> datetime:
    return NOW - timedelta(days=spec.days_ago)


def test_timeline_is_ordered_and_not_in_future() -> None:
    for spec in demo_data.build_plan():
        steps = demo_data.timeline(spec, NOW)
        assert steps[0].status is ReportStatus.RECEIVED
        times = [s.at for s in steps]
        assert times == sorted(times)
        assert times[-1] <= NOW, spec.description


def test_every_spec_uses_known_names() -> None:
    from app.models.enums import Priority

    for spec in demo_data.build_plan():
        assert spec.category in demo_data.DEPT_BY_CATEGORY, spec.category
        assert Priority(spec.priority)


def test_open_reports_are_within_sla() -> None:
    plan = demo_data.build_plan()
    open_specs = [s for s in plan if demo_data.final_status(s, NOW) in OPEN]
    assert len(open_specs) >= 8
    for spec in open_specs:
        deadline = _created(spec) + timedelta(hours=DEFAULT_SLA_HOURS[spec.priority])
        assert deadline > NOW, f"마감 초과 상태로 시작하면 SLA 배치가 알림을 보냄: {spec.description}"


def test_sla_compliance_is_realistic() -> None:
    met = breached = 0
    for spec in demo_data.build_plan():
        steps = demo_data.timeline(spec, NOW)
        resolved = next((s.at for s in steps if s.status is ReportStatus.RESOLVED), None)
        if resolved is None:
            continue
        deadline = _created(spec) + timedelta(hours=DEFAULT_SLA_HOURS[spec.priority])
        if resolved <= deadline:
            met += 1
        else:
            breached += 1
    assert breached >= 3
    assert 0.80 <= met / (met + breached) <= 0.95


def test_detection_candidates_are_exactly_the_two_bursts() -> None:
    """같은 건물+세부위치+카테고리 72시간 안 3건 이상 — 시연용 후보 2곳만."""
    groups: dict[tuple[str, str | None, str], list[datetime]] = defaultdict(list)
    for spec in demo_data.build_plan():
        groups[(spec.building, spec.detail, spec.category)].append(_created(spec))
    bursting = set()
    for key, times in groups.items():
        times.sort()
        for i, start in enumerate(times):
            if len([t for t in times[i:] if t - start <= timedelta(hours=72)]) >= 3:
                bursting.add(key)
    assert bursting == {("은주1관", "화장실", "시설·설비"), ("한림관", "강의실", "IT·네트워크")}


def test_predictions_point_to_the_future() -> None:
    """건물+카테고리 3건 이상이면 예측이 생긴다 — 평균 간격이 하루 이상이고 예상 시점이 아직 안 지난 상태여야 자연스럽다."""
    groups: dict[tuple[str, str], list[datetime]] = defaultdict(list)
    for spec in demo_data.build_plan():
        groups[(spec.building, spec.category)].append(_created(spec))
    predicted = {k: sorted(v) for k, v in groups.items() if len(v) >= 3}
    assert len(predicted) >= 5
    for key, times in predicted.items():
        gaps = [(b - a).total_seconds() / 86400 for a, b in pairwise(times)]
        avg = sum(gaps) / len(gaps)
        assert avg >= 1.0, key
        assert times[-1] + timedelta(days=avg) > NOW, key


def test_scale() -> None:
    plan = demo_data.build_plan()
    assert 50 <= len(plan) <= 80
