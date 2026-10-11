"""조건-액션 규칙 테스트 (작업 1-21, 명세서 3-1·5-1 `automation-rules`).

- 어떤 규칙이 고르는지(matching_rule) 단위테스트: DB 없이 항상 실행
- /admin/config/automation-rules·접수 적용 통합테스트: TEST_DATABASE_URL(마이그레이션 끝난 테스트용 빈 DB)이
  있을 때만 — 운영 DB 금지! 규칙이 켜져 있으면 다른 테스트의 신고 우선순위가 바뀌므로 테스트마다 빈 목록으로 되돌린다.
"""
import os
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest

TEST_DB = os.environ.get("TEST_DATABASE_URL")
if TEST_DB:
    os.environ["DATABASE_URL"] = TEST_DB
    os.environ["JWT_SECRET"] = "integration-test-secret-at-least-32-bytes!!"

import bcrypt
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db.session import get_engine, get_sessionmaker
from app.main import app
from app.models import Admin, AutomationRule, ChatSession
from app.models.enums import Level, Priority
from app.services.report_service import create_report, matching_rule, rule_note
from app.services.slot_filling import ReportSlots

CAT, OTHER_CAT = uuid.uuid4(), uuid.uuid4()
BLD, OTHER_BLD = uuid.uuid4(), uuid.uuid4()


def rule(priority: Priority, building: uuid.UUID | None = None, category: uuid.UUID = CAT,
         active: bool = True, name: str = "r") -> AutomationRule:
    return AutomationRule(name=name, category_id=category, building_id=building,
                          resulting_priority=priority, is_active=active)


# ── 단위테스트: 규칙 고르기 (명세 3-1) ──

def test_matching_rule_picks_by_spec() -> None:
    every = rule(Priority.P1, name="모든 건물")
    here = rule(Priority.P3, building=BLD, name="이 건물")
    assert matching_rule([every, here], CAT, BLD) is here          # 건물 지정 규칙이 우선 (우선순위가 낮아도)
    assert matching_rule([every, here], CAT, OTHER_BLD) is every   # 다른 건물이면 모든 건물 규칙
    assert matching_rule([every, here], CAT, None) is every        # 건물을 모르는 신고도 모든 건물 규칙은 맞음
    assert matching_rule([every], OTHER_CAT, BLD) is None          # 카테고리가 다르면 없음
    assert matching_rule([rule(Priority.P1, active=False)], CAT, BLD) is None  # 꺼진 규칙은 무시


def test_matching_rule_tie_uses_higher_priority() -> None:
    low = rule(Priority.P4, building=BLD, name="낮음")
    high = rule(Priority.P2, building=BLD, name="높음")
    assert matching_rule([low, high], CAT, BLD) is high


def test_rule_note() -> None:
    assert rule_note(None) == ""
    assert rule_note(rule(Priority.P1, name="안전 즉시 P1")) == " 규칙 '안전 즉시 P1' 적용."


# ── 통합테스트 ──

needs_db = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL 없음 — DB 통합테스트 생략")

PASSWORD = "test-only-password"
C = "/api/v1/admin/config"
TAG = uuid.uuid4().hex[:6]


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def headers(client: TestClient) -> dict[str, str]:
    db = get_sessionmaker()()
    a = Admin(login_id=f"t_{uuid.uuid4().hex[:8]}",
              password_hash=bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(4)).decode(), name="테스트관리자")
    db.add(a)
    db.commit()
    login_id = a.login_id
    db.close()
    res = client.post("/api/v1/admin/auth/login", json={"login_id": login_id, "password": PASSWORD})
    assert res.status_code == 200
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


@pytest.fixture()
def refs(client: TestClient, headers: dict[str, str]) -> Iterator[dict[str, Any]]:
    """카테고리 2개·건물 2개 id. 끝나면 규칙을 모두 지움(다른 테스트의 접수 우선순위에 영향 없게)."""
    cats = client.get(f"{C}/categories", headers=headers).json()["items"]
    blds = client.get(f"{C}/buildings", headers=headers).json()["items"]
    active = [c for c in cats if c["is_active"]]
    yield {"cat": active[0], "cat2": active[1], "bld": blds[0], "bld2": blds[1]}
    assert client.put(f"{C}/automation-rules", json={"items": []}, headers=headers).status_code == 200


def _put(client: TestClient, headers: dict[str, str], items: list[dict[str, Any]]) -> Any:
    return client.put(f"{C}/automation-rules", json={"items": items}, headers=headers)


@needs_db
def test_require_token(client: TestClient) -> None:
    assert client.get(f"{C}/automation-rules").status_code == 401
    assert client.put(f"{C}/automation-rules", json={"items": []}).status_code == 401


@needs_db
def test_save_update_delete(client: TestClient, headers: dict[str, str], refs: dict[str, Any]) -> None:
    cat, bld = refs["cat"], refs["bld"]
    res = _put(client, headers, [
        {"name": f"전체-{TAG}", "category_id": cat["id"], "building_id": None, "resulting_priority": "P1"},
        {"name": f"건물-{TAG}", "category_id": cat["id"], "building_id": bld["id"], "resulting_priority": "P3",
         "is_active": False},
    ])
    assert res.status_code == 200, res.text
    items = res.json()["items"]
    assert [i["name"] for i in items] == [f"전체-{TAG}", f"건물-{TAG}"]
    assert set(items[0]) == {"id", "name", "category", "building", "resulting_priority", "is_active"}
    assert items[0]["category"] == {"id": cat["id"], "name": cat["name"]} and items[0]["building"] is None
    assert items[1]["building"] == {"id": bld["id"], "name": bld["name"]} and items[1]["is_active"] is False
    assert client.get(f"{C}/automation-rules", headers=headers).json() == res.json()

    # 첫 규칙은 이름·우선순위 수정(id 유지), 둘째는 빠졌으니 삭제, 하나 새로 추가
    first = items[0]
    res = _put(client, headers, [
        {"id": first["id"], "name": f"전체2-{TAG}", "category_id": cat["id"], "building_id": None,
         "resulting_priority": "P2"},
        {"name": f"새-{TAG}", "category_id": refs["cat2"]["id"], "building_id": None, "resulting_priority": "P4"},
    ])
    assert res.status_code == 200, res.text
    items = res.json()["items"]
    assert [(i["id"] == first["id"], i["name"], i["resulting_priority"]) for i in items] == [
        (True, f"전체2-{TAG}", "P2"), (False, f"새-{TAG}", "P4")]


@needs_db
def test_swap_conditions_between_rules(client: TestClient, headers: dict[str, str], refs: dict[str, Any]) -> None:
    """두 규칙의 조건·이름을 맞바꿔도 UNIQUE에 걸리지 않음."""
    a = {"name": f"A-{TAG}", "category_id": refs["cat"]["id"], "building_id": None, "resulting_priority": "P1"}
    b = {"name": f"B-{TAG}", "category_id": refs["cat2"]["id"], "building_id": None, "resulting_priority": "P2"}
    items = _put(client, headers, [a, b]).json()["items"]
    swapped = [{**a, "id": items[0]["id"], "name": b["name"], "category_id": b["category_id"]},
               {**b, "id": items[1]["id"], "name": a["name"], "category_id": a["category_id"]}]
    res = _put(client, headers, swapped)
    assert res.status_code == 200, res.text
    assert [i["name"] for i in res.json()["items"]] == [b["name"], a["name"]]


@needs_db
def test_rule_violations(client: TestClient, headers: dict[str, str], refs: dict[str, Any]) -> None:
    cat = refs["cat"]["id"]
    base = {"name": f"규칙-{TAG}", "category_id": cat, "building_id": None, "resulting_priority": "P1"}
    assert _put(client, headers, [base, {**base, "name": f"다른-{TAG}"}]).status_code == 409   # 같은 조건 두 번
    other = {**base, "category_id": refs["cat2"]["id"]}
    assert _put(client, headers, [base, other]).status_code == 409                              # 이름 중복
    assert _put(client, headers, [{**base, "category_id": str(uuid.uuid4())}]).status_code == 422  # 없는 카테고리
    assert _put(client, headers, [{**base, "building_id": str(uuid.uuid4())}]).status_code == 422  # 없는 건물
    assert _put(client, headers, [{**base, "id": str(uuid.uuid4())}]).status_code == 409          # 없는 규칙 id
    assert _put(client, headers, [{**base, "resulting_priority": "P9"}]).status_code == 422
    assert _put(client, headers, [{**base, "name": " "}]).status_code == 422
    assert client.get(f"{C}/automation-rules", headers=headers).json() == {"items": []}  # 아무것도 저장 안 됨


@needs_db
def test_rule_changes_new_report_priority_and_sla(client: TestClient, headers: dict[str, str],
                                                  refs: dict[str, Any]) -> None:
    """규칙이 맞으면 매트릭스 결과(저저 = P4) 대신 규칙 우선순위로 접수하고 SLA 마감도 그 기준."""
    cat, bld = refs["cat"], refs["bld"]
    assert _put(client, headers, [{"name": f"즉시-{TAG}", "category_id": cat["id"], "building_id": bld["id"],
                                   "resulting_priority": "P1"}]).status_code == 200
    sla = {s["priority"]: s["sla_hours"] for s in client.get(f"{C}/sla", headers=headers).json()["items"]}

    db = get_sessionmaker()()
    session = ChatSession(user_identifier=f"t-{TAG}")
    db.add(session)
    db.flush()
    low = {"has_problem": True, "impact": Level.LOW, "urgency": Level.LOW, "category": cat["name"]}
    hit, _, applied = create_report(db, session.id, ReportSlots(**low, building_id=uuid.UUID(bld["id"])), "규칙 맞음")
    miss, _, none = create_report(db, session.id, ReportSlots(**low, building_id=uuid.UUID(refs["bld2"]["id"])),
                                  "다른 건물")
    db.commit()
    assert hit.priority == Priority.P1 and applied is not None and applied.name == f"즉시-{TAG}"
    assert hit.sla_deadline is not None
    assert abs(hit.sla_deadline - hit.created_at - timedelta(hours=sla["P1"])) < timedelta(minutes=1)
    assert miss.priority != Priority.P1 and none is None
    db.delete(hit)
    db.delete(miss)
    db.delete(session)
    db.commit()
    db.close()
