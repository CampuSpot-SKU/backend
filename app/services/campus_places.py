"""학교 위치 데이터(app/data/location_options.json) 조회 — 건물에 실제 있는 층·호실만 위치로 인정하기 위한 기준.

배경: 글자만 읽어서는 "청운관 강의실", "청운관 4층"처럼 학교에 없는 조합을 그대로 접수하게 된다.
그래서 건물 → 층 → 호실(이름·호수)을 학교 데이터와 대조한다. 데이터가 불완전할 수 있으므로 여기서는 조회만 하고,
"없다고 막을지 / 확인 후 받을지"는 slot_filling.py가 정한다 (확인 후 그대로 접수 + 관리자 확인 표시).
다른 학교에 적용할 땐 location_options.json만 교체하면 된다.
"""
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "location_options.json"
_PAREN_TAIL = re.compile(r"\s*\([^)]*\)\s*$")


@dataclass(frozen=True)
class Place:
    label: str  # 데이터의 표시 이름 (예: "강의실 (301)")
    room_no: str | None  # "301", "B101" (호수가 없는 화장실·입구 등은 None)
    use: str | None  # 용도 (예: "멀티미디어강의실")

    @property
    def core(self) -> str:
        """괄호 안 호수를 뺀 이름 ("강의실 (301)" → "강의실")."""
        return _PAREN_TAIL.sub("", self.label).strip()

    @property
    def display(self) -> str:
        """학생에게 보여줄 이름 ("301호 강의실"). 호수가 없으면 이름만."""
        return f"{self.room_no}호 {self.core}" if self.room_no else self.core

    @property
    def is_classroom(self) -> bool:
        return "강의실" in self.label or "강의" in (self.use or "")

    @property
    def is_toilet(self) -> bool:
        return "화장실" in self.label


@lru_cache(maxsize=1)
def _index() -> dict[str, dict[str, list[Place]]]:
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    index: dict[str, dict[str, list[Place]]] = {}
    for b in data.get("buildings", []):
        name = b.get("name")
        if not name or b.get("custom"):
            continue
        floors: dict[str, list[Place]] = {}
        for f in b.get("floors", []):
            floor = f.get("floor")
            if f.get("custom") or not floor:  # "층 구분 없음" 묶음은 층으로 치지 않음
                continue
            floors[str(floor)] = [
                Place(p["label"], p.get("room_no") or None, p.get("use"))
                for p in f.get("places", [])
                if not p.get("custom")
            ]
        index[name] = floors
    return index


def has_data(building: str | None) -> bool:
    """이 건물의 층 데이터가 있나 (없으면 층·호실 검증을 하지 않음)."""
    return bool(building) and bool(_index().get(building or ""))


def floors(building: str) -> list[str]:
    return list(_index().get(building, {}))


def has_floor(building: str | None, floor: str | None) -> bool | None:
    """그 건물에 그 층이 있나. 데이터가 없어서 모르면 None."""
    if not building or not floor or not has_data(building):
        return None
    return floor in _index()[building]


def places(building: str | None, floor: str | None) -> list[Place]:
    if not building or not floor:
        return []
    return list(_index().get(building, {}).get(floor, []))


def building_places(building: str | None) -> list[Place]:
    if not building:
        return []
    return [p for ps in _index().get(building, {}).values() for p in ps]


def has_classroom(building: str | None, floor: str | None = None) -> bool:
    ps = places(building, floor) if floor and has_floor(building, floor) else building_places(building)
    return any(p.is_classroom for p in ps)


def find_room(building: str | None, floor: str | None, room_no: str) -> Place | None:
    """그 층에 그 호수가 있나 (없으면 None)."""
    wanted = room_no.upper()
    for p in places(building, floor):
        if p.room_no and p.room_no.upper() == wanted:
            return p
    return None


def find_by_name(building: str | None, floor: str | None, line: str) -> Place | None:
    """호수 없이 이름으로 말한 호실 ("학생과", "보건실") — 그 층에서 딱 하나로 좁혀질 때만."""
    text = re.sub(r"[\s.,!?~]+", " ", line).strip()
    compact = text.replace(" ", "")
    if len(compact) < 2:
        return None
    hits = [
        p for p in places(building, floor)
        if not p.is_toilet and p.core
        and (p.core in text or (len(compact) >= 3 and compact in p.core.replace(" ", "")))
    ]
    cores = {p.core for p in hits}
    return hits[0] if len(hits) == 1 or (hits and len(cores) == 1 and len(hits) == 1) else None


def floor_label(floor: str) -> str:
    return f"지하 {floor[1:]}층" if floor.startswith("B") else f"{floor}층"


def floors_text(building: str) -> str:
    """"지하 1층, 1~3층, 5~11층"처럼 있는 층을 줄여서 보여주는 문구."""
    parts: list[str] = []
    run: list[int] = []

    def flush() -> None:
        if not run:
            return
        parts.append(f"{run[0]}층" if len(run) == 1 else f"{run[0]}~{run[-1]}층")
        run.clear()

    for f in floors(building):
        if f.startswith("B"):
            flush()
            parts.append(floor_label(f))
        elif run and int(f) == run[-1] + 1:
            run.append(int(f))
        else:
            flush()
            run.append(int(f))
    flush()
    return ", ".join(parts)


_GENERIC_NAMES = ("강의실", "화장실", "복도", "계단", "엘리베이터", "사무실", "실습실", "실험실", "연구실", "교수실")


@lru_cache(maxsize=1)
def _unique_names() -> dict[str, tuple[str, str, Place]]:
    """학교 전체에서 이름이 하나뿐인 장소 → (건물, 층, 장소). "공연장"처럼 이름만으로 위치가 정해지는 곳."""
    seen: dict[str, list[tuple[str, str, Place]]] = {}
    for building, floors_ in _index().items():
        for floor, ps in floors_.items():
            for p in ps:
                core = p.core.replace(" ", "")
                if len(core) >= 3 and core not in _GENERIC_NAMES and not p.is_toilet:
                    seen.setdefault(core, []).append((building, floor, p))
    return {k: v[0] for k, v in seen.items() if len(v) == 1}


def find_unique(text: str) -> tuple[str, str, Place] | None:
    """문장에 학교에서 유일한 장소 이름이 있으면 (건물, 층, 장소). 긴 이름 우선."""
    compact = text.replace(" ", "")
    for name in sorted(_unique_names(), key=len, reverse=True):
        if name in compact:
            return _unique_names()[name]
    return None


def has_type(building: str | None, floor: str | None, word: str) -> bool:
    """그 층(층을 모르거나 없는 층이면 건물 전체)에 "강의실" 같은 종류의 방이 있나. 데이터가 없으면 있다고 봄."""
    if not has_data(building):
        return True
    ps = places(building, floor) if floor and has_floor(building, floor) else building_places(building)
    if word == "강의실":
        return any(p.is_classroom for p in ps)
    return any(word in p.label for p in ps)
