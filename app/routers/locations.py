"""위치 선택 목록 — 접수 폼의 건물 → 층 → 세부장소 선택 UI용 (작업 1-3c, 명세서 5-1).

데이터는 app/data/location_options.json (team-docs/data/campus/ 에서 만든 것). 다른 학교에 적용할 땐
이 파일과 app/data/campus.json, 건물 시드만 바꾸면 된다. 단계마다 맨 끝에
`{"label": "목록에 없음 (직접 입력)", "custom": true}` 항목이 있음 — 고르면 학생이 직접 입력.
"""
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Response

router = APIRouter(tags=["locations"])

_PATH = Path(__file__).resolve().parent.parent / "data" / "location_options.json"


@lru_cache(maxsize=1)
def _load() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(_PATH.read_text(encoding="utf-8"))
    return data


@router.get("/locations")
def get_locations(response: Response) -> dict[str, Any]:
    """`{buildings: [{name, floors: [{floor, label, places: [{label, room_no?, use?, custom?}]}]}]}`"""
    response.headers["Cache-Control"] = "public, max-age=3600"  # 거의 안 바뀌는 데이터
    return _load()
