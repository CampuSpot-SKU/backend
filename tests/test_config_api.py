"""관리자 설정 API 통합테스트 — 카테고리·건물·우선순위 매트릭스·SLA·탐지 임계치 (작업 1-17).

TEST_DATABASE_URL(마이그레이션 끝난 테스트용 빈 DB)이 있을 때만 실행 — 운영 DB 금지!
설정을 바꾸는 테스트라서, 같은 DB를 쓰는 다른 테스트에 영향이 없게 끝나면 원래 값으로 되돌린다
(새로 만든 카테고리는 지울 수 없으므로 꺼 둔다 — 실행마다 다른 이름).
"""
import os
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import bcrypt
import pytest

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL 없음 — DB 통합테스트 생략")

if TEST_DB:
    os.environ["DATABASE_URL"] = TEST_DB
    os.environ["JWT_SECRET"] = "integration-test-secret-at-least-32-bytes!!"

from fastapi.testclient import TestClient

from app.config import get_settings
from app.db.session import get_engine, get_sessionmaker
from app.main import app
from app.models import Admin, Category, ChatSession, Report
from app.models.enums import Level, Priority, ReportStatus
from app.services.report_service import create_report, load_buildings, load_category_names
from app.services.slot_filling import ReportSlots

PASSWORD = "test-only-password"
C = "/api/v1/admin/config"
TAG = uuid.uuid4().hex[:6]  # 같은 테스트 DB에서 여러 번 돌려도 이름이 안 겹치게


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


@pytest.fixture(autouse=True)
def restore(client: TestClient, headers: dict[str, str]) -> Iterator[None]:
    """테스트마다 설정을 원래대로 되돌림 (다른 테스트 모듈이 시드값을 기대함)."""
    before = {p: client.get(f"{C}/{p}", headers=headers).json()
              for p in ("categories", "buildings", "priority-matrix", "sla", "detection")}
    yield
    cats = before["categories"]["items"]
    known = {c["id"] for c in cats}
    extra = [{**c, "is_active": False} for c in client.get(f"{C}/categories", headers=headers).json()["items"]
             if c["id"] not in known]  # 테스트가 추가한 카테고리는 끄기만 가능
    for path, body in (("categories", {"items": cats + extra}), ("buildings", before["buildings"]),
                       ("priority-matrix", before["priority-matrix"]), ("sla", before["sla"]),
                       ("detection", before["detection"])):
        res = client.put(f"{C}/{path}", json=body, headers=headers)
        assert res.status_code == 200, (path, res.text)


def _get(client: TestClient, headers: dict[str, str], path: str) -> Any:
    res = client.get(f"{C}/{path}", headers=headers)
    assert res.status_code == 200
    return res.json()


def test_require_token(client: TestClient) -> None:
    for p in ("categories", "buildings", "priority-matrix", "sla", "detection"):
        assert client.get(f"{C}/{p}").status_code == 401
        assert client.put(f"{C}/{p}", json={"items": []}).status_code == 401


def test_get_shapes(client: TestClient, headers: dict[str, str]) -> None:
    cats = _get(client, headers, "categories")["items"]
    assert any(c["name"] == "기타" and c["is_active"] for c in cats)
    assert set(cats[0]) == {"id", "name", "is_active"}
    blds = _get(client, headers, "buildings")["items"]
    assert len(blds) >= 12 and set(blds[0]) == {"id", "name", "aliases"}
    matrix = _get(client, headers, "priority-matrix")["items"]
    assert [(m["impact"], m["urgency"]) for m in matrix] == [("고", "고"), ("고", "저"), ("저", "고"), ("저", "저")]
    sla = _get(client, headers, "sla")["items"]
    assert [s["priority"] for s in sla] == ["P1", "P2", "P3", "P4"]
    assert set(sla[0]) == {"priority", "sla_hours", "escalation_50pct_action", "escalation_100pct_action",
                           "escalation_150pct_action"}
    det = _get(client, headers, "detection")
    assert set(det) == {"threshold_count", "threshold_hours"}


def test_categories_add_rename_deactivate(client: TestClient, headers: dict[str, str]) -> None:
    cats = _get(client, headers, "categories")["items"]
    target = next(c for c in cats if c["name"] != "기타")
    body = [c if c["id"] != target["id"] else {**c, "name": f"{c['name']}-{TAG}", "is_active": False} for c in cats]
    body.append({"name": f"새분류-{TAG}"})  # id 없음 = 추가, is_active 기본 true
    res = client.put(f"{C}/categories", json={"items": body}, headers=headers)
    assert res.status_code == 200, res.text
    by_name = {c["name"]: c for c in res.json()["items"]}
    assert by_name[f"{target['name']}-{TAG}"]["is_active"] is False
    assert by_name[f"새분류-{TAG}"]["is_active"] is True

    # 새 신고 분류 후보(AI에 넘기는 목록)에 바로 반영: 켠 것은 들어가고 끈 것은 빠짐
    db = get_sessionmaker()()
    names = load_category_names(db)
    db.close()
    assert f"새분류-{TAG}" in names and f"{target['name']}-{TAG}" not in names


def test_categories_swap_names(client: TestClient, headers: dict[str, str]) -> None:
    cats = _get(client, headers, "categories")["items"]
    a, b = [c for c in cats if c["name"] != "기타"][:2]
    body = [{**c, "name": b["name"]} if c["id"] == a["id"] else {**c, "name": a["name"]} if c["id"] == b["id"] else c
            for c in cats]
    res = client.put(f"{C}/categories", json={"items": body}, headers=headers)
    assert res.status_code == 200, res.text
    by_id = {c["id"]: c["name"] for c in res.json()["items"]}
    assert by_id[a["id"]] == b["name"] and by_id[b["id"]] == a["name"]


def test_categories_rules(client: TestClient, headers: dict[str, str]) -> None:
    cats = _get(client, headers, "categories")["items"]

    def put(items: list[dict[str, Any]]) -> int:
        return client.put(f"{C}/categories", json={"items": items}, headers=headers).status_code

    assert put(cats[1:]) == 409  # 기존 항목을 빼면(=삭제) 409
    assert put(cats + [{"name": cats[0]["name"]}]) == 409  # 이름 중복
    assert put(cats + [{"id": str(uuid.uuid4()), "name": f"x-{TAG}"}]) == 409  # 없는 id
    assert put([{**c, "is_active": False} if c["name"] == "기타" else c for c in cats]) == 409  # 기본 카테고리 끄기
    assert put([{**c, "name": f"기타2-{TAG}"} if c["name"] == "기타" else c for c in cats]) == 409  # 기본 이름 바꾸기
    assert put([{**c, "is_active": False} for c in cats if c["name"] != "기타"]
               + [c for c in cats if c["name"] == "기타"]) == 200  # 기타만 켜져 있어도 됨
    assert put(cats + [{"name": "   "}]) == 422  # 빈 이름
    assert put([]) == 422
    # 409가 나도 아무것도 저장 안 됨 (전부 아니면 전무)
    assert put(cats + [{"name": f"추가-{TAG}"}, {"name": cats[0]["name"]}]) == 409
    assert all(c["name"] != f"추가-{TAG}" for c in _get(client, headers, "categories")["items"])


def test_buildings_add_rename_delete(client: TestClient, headers: dict[str, str]) -> None:
    blds = _get(client, headers, "buildings")["items"]
    new_name = f"테스트관-{TAG}"
    res = client.put(f"{C}/buildings", headers=headers,
                     json={"items": blds + [{"name": new_name, "aliases": [" 테관 ", "테관", f"T{TAG}"]}]})
    assert res.status_code == 200, res.text
    added = next(b for b in res.json()["items"] if b["name"] == new_name)
    assert added["aliases"] == ["테관", f"T{TAG}"]  # 앞뒤 공백 제거·중복 제거

    # 챗봇 위치 매칭(접수 때 읽는 건물 목록)에 바로 반영
    db = get_sessionmaker()()
    assert any(b.name == new_name and "테관" in b.aliases for b in load_buildings(db))
    db.close()

    # 이름 바꾸기 + 두 건물 이름 맞바꾸기 (UNIQUE 충돌 없이)
    cur = res.json()["items"]
    x, y = [b for b in cur if b["name"] != new_name][:2]
    swapped = [{**b, "name": y["name"]} if b["id"] == x["id"] else {**b, "name": x["name"]} if b["id"] == y["id"]
               else {**b, "name": f"{new_name}-수정"} if b["id"] == added["id"] else b for b in cur]
    res = client.put(f"{C}/buildings", json={"items": swapped}, headers=headers)
    assert res.status_code == 200, res.text
    by_id = {b["id"]: b["name"] for b in res.json()["items"]}
    assert by_id[x["id"]] == y["name"] and by_id[y["id"]] == x["name"] and by_id[added["id"]] == f"{new_name}-수정"

    # 신고가 안 쓴 건물은 목록에서 빼면 삭제됨
    res = client.put(f"{C}/buildings", json={"items": [b for b in res.json()["items"] if b["id"] != added["id"]]},
                     headers=headers)
    assert res.status_code == 200
    assert all(b["id"] != added["id"] for b in res.json()["items"])


def test_buildings_in_use_cannot_be_deleted(client: TestClient, headers: dict[str, str]) -> None:
    blds = _get(client, headers, "buildings")["items"]
    res = client.put(f"{C}/buildings", json={"items": blds + [{"name": f"쓰인관-{TAG}"}]}, headers=headers)
    used = next(b for b in res.json()["items"] if b["name"] == f"쓰인관-{TAG}")
    db = get_sessionmaker()()
    cat = db.query(Category).filter(Category.name == "기타").one()
    report = Report(category_id=cat.id, priority=Priority.P4, status=ReportStatus.RECEIVED,
                    description="테스트 신고(설정)", building_id=uuid.UUID(used["id"]))
    db.add(report)
    db.commit()
    db.close()

    rest = [b for b in res.json()["items"] if b["id"] != used["id"]]
    res = client.put(f"{C}/buildings", json={"items": rest}, headers=headers)
    assert res.status_code == 409 and f"쓰인관-{TAG}" in res.json()["detail"]
    assert any(b["id"] == used["id"] for b in _get(client, headers, "buildings")["items"])  # 그대로 남음

    # 정리: 신고를 지운 뒤 건물도 지움 (restore가 원래 목록으로 되돌릴 수 있게)
    db = get_sessionmaker()()
    db.query(Report).filter(Report.building_id == uuid.UUID(used["id"])).delete()
    db.commit()
    db.close()

    def put(items: list[dict[str, Any]]) -> int:
        return client.put(f"{C}/buildings", json={"items": items}, headers=headers).status_code

    assert put(blds + [{"name": blds[0]["name"]}]) == 409  # 이름 중복
    assert put(blds + [{"id": str(uuid.uuid4()), "name": f"없는-{TAG}"}]) == 409  # 없는 id


def test_matrix_and_sla_apply_to_new_reports(client: TestClient, headers: dict[str, str]) -> None:
    """설정만 바꾸면 다음 신고부터 반영 — 확장성 시연의 핵심."""
    matrix = _get(client, headers, "priority-matrix")["items"]
    changed = [{**m, "resulting_priority": "P4"} if (m["impact"], m["urgency"]) == ("고", "고") else m for m in matrix]
    assert client.put(f"{C}/priority-matrix", json={"items": changed}, headers=headers).status_code == 200
    sla = _get(client, headers, "sla")["items"]
    sla2 = [{**s, "sla_hours": 5} if s["priority"] == "P4" else s for s in sla]
    res = client.put(f"{C}/sla", json={"items": sla2}, headers=headers)
    assert res.status_code == 200 and res.json()["items"][3]["sla_hours"] == 5

    db = get_sessionmaker()()
    session = ChatSession(user_identifier=f"t-{TAG}")
    db.add(session)
    db.flush()
    report, _ = create_report(db, session.id, ReportSlots(has_problem=True, impact=Level.HIGH, urgency=Level.HIGH),
                              "테스트 신고(설정 반영)")
    db.commit()
    assert report.priority == Priority.P4  # 원래 고고 = P1
    assert report.sla_deadline is not None
    # created_at은 DB 시각·마감은 앱 시각 기준이라 몇 초 차이는 허용 (원래 P1이면 4시간)
    assert abs(report.sla_deadline - report.created_at - timedelta(hours=5)) < timedelta(minutes=1)
    db.delete(report)
    db.commit()
    db.close()


def test_matrix_sla_detection_rules(client: TestClient, headers: dict[str, str]) -> None:
    matrix = _get(client, headers, "priority-matrix")["items"]
    dup = matrix[:3] + [matrix[0]]  # 4칸이지만 고고가 두 번
    assert client.put(f"{C}/priority-matrix", json={"items": dup}, headers=headers).status_code == 409
    assert client.put(f"{C}/priority-matrix", json={"items": matrix[:3]}, headers=headers).status_code == 422
    bad = [{**matrix[0], "resulting_priority": "P9"}] + matrix[1:]
    assert client.put(f"{C}/priority-matrix", json={"items": bad}, headers=headers).status_code == 422

    sla = _get(client, headers, "sla")["items"]
    assert client.put(f"{C}/sla", json={"items": sla[:3] + [sla[0]]}, headers=headers).status_code == 409
    assert client.put(f"{C}/sla", json={"items": sla[:3]}, headers=headers).status_code == 422
    zero = [{**sla[0], "sla_hours": 0}] + sla[1:]
    assert client.put(f"{C}/sla", json={"items": zero}, headers=headers).status_code == 422
    blank = [{**sla[0], "escalation_50pct_action": ""}] + sla[1:]
    res = client.put(f"{C}/sla", json={"items": blank}, headers=headers)
    assert res.status_code == 200 and res.json()["items"][0]["escalation_50pct_action"] is None  # 빈 문구 = 없음

    res = client.put(f"{C}/detection", json={"threshold_count": 4, "threshold_hours": 48}, headers=headers)
    assert res.status_code == 200 and res.json() == {"threshold_count": 4, "threshold_hours": 48}
    assert _get(client, headers, "detection") == {"threshold_count": 4, "threshold_hours": 48}
    assert client.put(f"{C}/detection", json={"threshold_count": 1, "threshold_hours": 48},
                      headers=headers).status_code == 422
    assert client.put(f"{C}/detection", json={"threshold_count": 3, "threshold_hours": 0},
                      headers=headers).status_code == 422
