"""데모 데이터 시나리오 검사 — DB 없이 도는 테스트 (작업 S-2).

시연 화면이 의도대로 나오는지(탐지 후보 6곳, 예측 날짜가 미래, SLA 준수율, 처리 중 신고는 마감 안)를
계획 데이터만으로 확인한다. 실제 삽입은 임시 DB로 따로 돌려 본다.
"""

import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest

from app.models.enums import ChatIntent, ChatRole, ReportStatus
from app.services import campus_places as cp
from app.services import demo_data
from app.services.conversation_query import pick_flow
from app.services.demo_data import DEFAULT_SLA_HOURS, KST, DemoSpec
from app.services.pii_mask import mask_pii
from app.services.slot_filling import DONE_PREFIX

NOW = datetime(2026, 10, 31, 20, 0, tzinfo=UTC)  # 2026-11-01 05:00 KST — 시딩한 시각
OPEN = {ReportStatus.RECEIVED, ReportStatus.ASSIGNED, ReportStatus.IN_PROGRESS}


def _created(spec: DemoSpec) -> datetime:
    return demo_data.created_at_for(spec, NOW)


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
        assert deadline > NOW, (
            f"마감 초과 상태로 시작하면 SLA 배치가 알림을 보냄: {spec.description}"
        )


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
    assert 0.80 <= met / (met + breached) <= 0.97


def test_detection_candidates_are_exactly_the_six_bursts() -> None:
    """같은 건물+세부위치+카테고리 72시간 안 3건 이상 — 시연용 후보 6곳만(그 밖의 중복 접수 쌍은 2건 이하)."""
    groups: dict[tuple[str, str | None, str], list[datetime]] = defaultdict(list)
    for spec in demo_data.build_plan():
        if spec.building and spec.detail:  # 건물·세부위치가 없는 신고는 탐지에서 빠짐
            groups[(spec.building, spec.detail, spec.category)].append(_created(spec))
    bursting = set()
    for key, times in groups.items():
        times.sort()
        for i, start in enumerate(times):
            if len([t for t in times[i:] if t - start <= timedelta(hours=72)]) >= 3:
                bursting.add(key)
    assert bursting == {
        ("은주1관", "여자 화장실", "시설·설비"),
        ("혜인관", "301호 강의실", "IT·네트워크"),
        ("유담관", "B101호 주차장", "전기"),
        ("문예관", "엘리베이터", "시설·설비"),
        ("유담관", "1103호 L03", "시설·설비"),
        ("한림관", "201호 교직원 및 학생식당", "안전"),
    }


def test_predictions_point_to_the_future() -> None:
    """건물+카테고리 3건 이상이면 예측이 생긴다 — 평균 간격이 하루 이상이고 예상 시점이 아직 안 지난 상태여야 자연스럽다."""
    groups: dict[tuple[str, str], list[datetime]] = defaultdict(list)
    for spec in demo_data.build_plan():
        if spec.building:
            groups[(spec.building, spec.category)].append(_created(spec))
    predicted = {k: sorted(v) for k, v in groups.items() if len(v) >= 3}
    assert len(predicted) >= 30
    for key, times in predicted.items():
        gaps = [(b - a).total_seconds() / 86400 for a, b in pairwise(times)]
        avg = sum(gaps) / len(gaps)
        assert avg >= 1.0, key
        assert times[-1] + timedelta(days=avg) > NOW, key


def test_scale() -> None:
    plan = demo_data.build_plan()
    assert 300 <= len(plan) <= 420
    assert {s.building for s in plan if s.building} == set(demo_data.DEMO_BUILDINGS)
    assert {s.category for s in plan} == set(demo_data.DEPT_BY_CATEGORY)
    assert {"P1", "P2", "P4"} <= {
        s.priority for s in plan
    }  # 실제 AI는 P3를 거의 내지 않는다(영향 낮음·긴급 높음)


def test_near_simultaneous_duplicates_exist() -> None:
    """같은 날 같은 곳에서 비슷한 시간에 다시 들어온 중복 접수가 여럿 있다 — 탐지 후보(72시간 3건)는 따로 6곳뿐이다."""
    groups: dict[tuple[str, str | None, str], list[datetime]] = defaultdict(list)
    for spec in demo_data.build_plan():
        if spec.building and spec.detail:
            groups[(spec.building, spec.detail, spec.category)].append(_created(spec))
    dup = 0
    for times in groups.values():
        times.sort()
        dup += any(b - a <= timedelta(hours=3) for a, b in pairwise(times))
    assert dup >= 10


def test_mine_descriptions_exist_once_in_plan() -> None:
    """학생 계정에 연결할 8건이 계획에 정확히 한 번씩 있고, 상태가 골고루 섞여 있다."""
    descs = [s.description for s in demo_data.build_plan()]
    assert len(demo_data.MINE_DESCRIPTIONS) == 8
    for d in demo_data.MINE_DESCRIPTIONS:
        assert descs.count(d) == 1, d
    mine = [s for s in demo_data.build_plan() if s.description in demo_data.MINE_DESCRIPTIONS]
    finals = {demo_data.final_status(s, NOW) for s in mine}
    assert len(finals) >= 2


# ── S-2b: 접수 시각 흩뿌리기 ────────────────────────────────────────────


def test_created_times_are_spread_over_student_hours() -> None:
    old = [s for s in demo_data.build_plan() if s.days_ago >= demo_data.SPREAD_FROM_DAYS]
    assert len(old) >= 100
    clock = set()
    for spec in old:
        created = _created(spec)
        local = created.astimezone(KST)
        assert 8 <= local.hour <= 21, spec.description
        assert created <= NOW - timedelta(days=demo_data.SPREAD_FROM_DAYS - 1)
        # 그날(KST)이 "며칠 전" 기준일과 같아야 간격이 크게 틀어지지 않음
        assert local.date() == (NOW - timedelta(days=spec.days_ago)).astimezone(KST).date()
        clock.add((local.hour, local.minute, local.second))
    assert len(clock) >= len(old) * 0.9  # 예전엔 44건이 같은 초였음


def test_reports_are_filed_at_their_clock_time() -> None:
    """시각(clock)이 정해진 신고는 그날(KST) 그 시각에 접수되고, 아직 오지 않은 시각이 아니다."""
    for spec in demo_data.build_plan():
        assert spec.clock
        created = _created(spec).astimezone(KST)
        assert f"{created.hour:02d}:{created.minute:02d}" == spec.clock
        assert created <= NOW, spec.description


def test_created_times_are_deterministic() -> None:
    plan = demo_data.build_plan()
    assert [_created(s) for s in plan] == [_created(s) for s in plan]


# ── S-2b: 대화 기록 ─────────────────────────────────────────────────────


@dataclass
class _Msg:
    role: ChatRole
    content: str
    intent: ChatIntent | None
    created_at: datetime


def test_every_report_has_a_dialogue_that_the_admin_viewer_can_pick() -> None:
    """관리자 "신고 대화"(conversation_query.pick_flow)가 대화 전체를 신고 구간으로 고르는지."""
    for i, spec in enumerate(demo_data.build_plan(), start=1):
        created = _created(spec)
        dialogue = demo_data.build_dialogue(spec, i, created)
        msgs = [_Msg(m.role, m.content, m.intent, m.at) for m in dialogue]
        flow, truncated = pick_flow(msgs, created)
        assert not truncated
        assert flow == msgs, spec.description
        assert msgs[0].role is ChatRole.USER
        assert msgs[-1].role is ChatRole.ASSISTANT and msgs[-1].intent is None
        assert msgs[-1].content.startswith(DONE_PREFIX) and f"접수번호는 {i}번" in msgs[-1].content
        assert all(m.intent is ChatIntent.REPORT for m in msgs[:-1])
        times = [m.created_at for m in msgs]
        assert times == sorted(times) and len(set(times)) == len(times)
        assert created - timedelta(seconds=2) <= times[-1] <= created + timedelta(minutes=1)
        assert times[0] < created - timedelta(seconds=8)


def test_building_is_always_said_by_the_student() -> None:
    """학생이 건물을 한 번도 말하지 않았는데 접수에 건물이 들어가면 안 된다 — 안 말했으면 챗봇이 되물어야 한다."""
    for i, spec in enumerate(demo_data.build_plan(), start=1):
        if spec.building is None:
            continue
        dialogue = demo_data.build_dialogue(spec, i, _created(spec))
        said = " ".join(m.content for m in dialogue if m.role is ChatRole.USER)
        unique = cp.find_unique(said)
        assert spec.building in said or (unique and unique[0] == spec.building), spec.description


def test_dialogues_have_variety_and_clarifying_questions() -> None:
    plan = demo_data.build_plan()
    dialogues = [demo_data.build_dialogue(s, i, _created(s)) for i, s in enumerate(plan, start=1)]
    firsts = {d[0].content for d in dialogues}
    assert len(firsts) >= len(plan) * 0.85
    lengths = {len(d) for d in dialogues}
    assert lengths >= {4, 6}, lengths  # 바로 접수(4개) / 되묻기 한 번(6개)가 섞여 있음
    asked = [d for d in dialogues if len(d) > 4]
    assert 15 <= len(asked) <= len(plan) * 0.6


def test_dialogue_text_is_clean() -> None:
    for i, spec in enumerate(demo_data.build_plan(), start=1):
        for m in demo_data.build_dialogue(spec, i, _created(spec)):
            assert mask_pii(m.content) == m.content, m.content  # 전화번호·학번 패턴이 섞이지 않음
            assert m.content.strip() == m.content and m.content


# ── 실제 DB (TEST_DATABASE_URL — 마이그레이션 끝난 테스트용 DB, 운영 DB 금지!) ──────────

TEST_DB = os.environ.get("TEST_DATABASE_URL")


@pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL 없음 — DB 통합테스트 생략")
def test_insert_plan_and_purge_chat_on_real_db() -> None:
    os.environ["DATABASE_URL"] = str(TEST_DB)
    from sqlalchemy import func, select, text

    from app.config import get_settings
    from app.db.session import get_engine, get_sessionmaker
    from app.models.chat import ChatMessage, ChatSession
    from app.models.report import Report
    from app.services.conversation_query import get_report_conversation

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    db = get_sessionmaker()()
    try:
        demo_data.reset_demo(db)
        plan = demo_data.build_plan()
        now = datetime.now(UTC)
        counts = demo_data.insert_plan(db, plan, now)
        db.commit()
        assert counts.reports == counts.sessions == len(plan)
        assert counts.messages >= len(plan) * 4
        assert demo_data.count_demo(db) == len(plan)

        # 관리자 신고 대화 열람이 실제로 대화를 돌려준다 (마지막 = 접수 완료 안내)
        report = db.scalars(
            select(Report).join(ChatSession, Report.session_id == ChatSession.id)
        ).first()
        assert report is not None
        conv = get_report_conversation(db, report.id)
        assert conv is not None and len(conv.messages) >= 4
        assert conv.messages[-1].content.startswith(DONE_PREFIX)
        assert f"{report.display_no}번" in conv.messages[-1].content

        # 학생 계정에 데모 대화 8건 연결 → 이전 대화 목록에 보인다 (계정이 없으면 0건)
        from app.models.user import User
        from app.services.chat_history import list_my_sessions

        assert demo_data.attach_demo_to_user(db, "no-such-student") == 0
        student = User(login_id="seedtest-stu", password_hash="x" * 60, name="테스트")
        db.add(student)
        db.flush()
        assert demo_data.attach_demo_to_user(db, "seedtest-stu") == 8
        assert (
            demo_data.attach_demo_to_user(db, "seedtest-stu") == 0
        )  # 이미 연결된 건 다시 안 건드림
        db.commit()
        mine = list_my_sessions(db, student.id, limit=20, offset=0)
        assert len(mine.items) == 8

        # purge_chat: 데모 세션은 남기고, 신고 없는 테스트 세션만 지운다
        stray = ChatSession(user_identifier="stray-test-session")
        db.add(stray)
        db.flush()
        db.add(
            ChatMessage(
                session_id=stray.id,
                role=ChatRole.USER,
                content="테스트 문의",
                intent=ChatIntent.INQUIRY,
            )
        )
        db.commit()
        sessions, messages = demo_data.count_orphan_chat(db)
        assert sessions >= 1 and messages >= 1
        assert demo_data.purge_orphan_chat(db) >= 1
        db.commit()
        assert demo_data.count_orphan_chat(db) == (0, 0)
        assert demo_data.count_demo(db) == len(plan)  # 데모는 그대로
        assert db.scalar(select(func.count()).select_from(ChatMessage)) >= counts.messages
    finally:
        demo_data.reset_demo(db)
        db.execute(text("DELETE FROM users WHERE login_id = 'seedtest-stu'"))
        db.commit()
        db.close()
