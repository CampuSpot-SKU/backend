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
from typing import Any

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


# ── 학생 말에서 학교 장소 찾기 (에이전트에게 줄 "학교 데이터 후보") ─────────────────────
# 글자가 정확히 같을 때만 찾던 방식("장문수 교수연구실"만 찾고 "장문수 교수실"은 못 찾음)을 넓힌다.
# 여기서는 후보만 모으고, 그게 학생이 말한 곳이 맞는지·묻지 않아도 되는지는 에이전트(ai)가 판단한다.
_PERSON_CORE_RE = re.compile(r"^([가-힣]{2,4})\s*교수\s*연구실$")
_NOT_OFFICE_AFTER = ("강의", "수업", "강좌", "세미나", "과제", "시험")  # "장문수 교수님 강의실"은 연구실이 아님


@dataclass(frozen=True)
class PlaceHit:
    label: str  # 학생에게 보여줄 이름 (예: "장문수 교수연구실 (606)")
    building: str  # 건물 이름 (건물 밖 장소면 빈 문자열)
    floor: str  # "6", "B1" (없으면 빈 문자열)
    kind: str  # room / facility / outdoor / landmark
    near: tuple[str, ...] = ()  # 건물이 아닌 장소(landmark)가 붙어 있거나 사이에 있는 건물들
    relation: str = ""  # between / attached / ... (landmark만)


def term_hints(text: str) -> list[str]:
    """학생 말에 나온 별칭·철자 변형 풀이 (예: "엘베 = 엘리베이터", "혜청사 = 혜인관 · 청운관 사이")."""
    compact = re.sub(r"\s+", "", text)
    out: list[str] = []
    for std, variants in _places_data().get("term_aliases", {}).items():
        used = [v for v in variants if v != std and v.replace(" ", "") in compact]
        if used:
            out.append(f"{', '.join(used)} = {std}")
    for p in _places_data()["places"]:
        if p["kind"] == "landmark" and p["core"] in compact:
            if p["near"]:
                out.append(f"{p['core']} = {' · '.join(p['near'])} {'사이' if p.get('relation') == 'between' else '근처'}")
            else:
                out.append(f"{p['core']} = {p['name']} — 건물이 아니라서 어느 건물인지 묻지 않음")
    out += _same_name_room_hints(compact)
    return out


@lru_cache(maxsize=1)
def _rooms_by_name() -> dict[str, dict[str, list[tuple[str, str]]]]:
    """방 이름 → 건물 → [(층, 호수)] — 같은 건물에 같은 이름의 방이 여러 개인 경우를 찾기 위한 표."""
    table: dict[str, dict[str, list[tuple[str, str]]]] = {}
    for p in _places_data()["places"]:
        core = p["core"].replace(" ", "")
        if p["kind"] != "room" or p["generic"] or len(core) < 2:
            continue
        table.setdefault(core, {}).setdefault(p["building"], []).append((p["floor"], p.get("room_no") or ""))
    return table


def _same_name_room_hints(compact: str) -> list[str]:
    out: list[str] = []
    for name, by_building in _rooms_by_name().items():
        if name not in compact:
            continue
        for building, rooms in by_building.items():
            if len(rooms) > 1 and building in compact:
                nos = ", ".join(f"{r}호" if r else f"{f}층" for f, r in rooms[:8])
                out.append(f"{building}에는 '{name}'이 {len(rooms)}곳 있음 ({nos}) — 호수·층을 모르면 한 곳으로 특정할 수 없음")
    return out[:4]


@lru_cache(maxsize=1)
def _places_data() -> dict[str, Any]:
    """정규화된 장소 목록(places.json) — scripts/build_places.py가 원본 데이터로 만든다."""
    return dict(json.loads((DATA_PATH.parent / "places.json").read_text(encoding="utf-8")))


@lru_cache(maxsize=1)
def _alias_index() -> tuple[dict[str, list[PlaceHit]], int]:
    """띄어쓰기를 뺀 이름·별칭 → 장소 후보 (색인). 학생 말에서는 이 색인을 글자 조각으로 조회한다."""
    raw: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for p in _places_data()["places"]:
        if p["kind"] == "person_office":
            keys = [(a, "person") for a in p["aliases"]]
        elif p["kind"] == "room":
            core = p["core"].replace(" ", "")
            keys = [] if p["generic"] or len(core) < 3 else [(core, "room")]
        else:
            keys = [(a.replace(" ", ""), p["kind"]) for a in p["aliases"]]
        for key, kind in keys:
            raw.setdefault(key, []).append((kind, p))
    index: dict[str, list[PlaceHit]] = {}
    for key, items in raw.items():
        spots = {(p["building"], p["floor"], p.get("room_no") or "") for _, p in items if p["kind"] == "room"}
        if items[0][0] == "room" and len(spots) > 1:  # 같은 이름의 방이 여러 개면(다른 건물·층·호수) 이름만으로는 특정되지 않음
            continue
        seen_spot: set[tuple[str, str, str]] = set()
        for kind, p in items:
            if (p["kind"], p["building"], p["floor"]) in seen_spot and p["kind"] in ("room", "facility"):
                continue
            seen_spot.add((p["kind"], p["building"], p["floor"]))
            hit = PlaceHit(
                p["name"] if p["kind"] != "facility" else p["core"], p["building"], p["floor"],
                {"person_office": "room", "landmark": "landmark"}.get(p["kind"], p["kind"]),
                tuple(p.get("near") or ()),
                p.get("relation") or "",
            )
            index.setdefault(key + "\x00" + kind, []).append(hit)
    flat: dict[str, list[PlaceHit]] = {}
    for composite, hits in index.items():
        flat.setdefault(composite.split("\x00")[0], []).extend(hits)
        flat[composite] = hits  # 종류별 키 ("장문수\x00person") — 연구실 뒤 말 규칙에 씀
    longest = max((len(k) for k in flat if "\x00" not in k), default=0)
    return flat, longest


def search_places(text: str, limit: int = 8) -> list[PlaceHit]:
    """학생 말에 나온 학교 장소·시설·교수 연구실 후보 (긴 이름 우선, 중복 제거).

    색인에서 글자 조각을 조회한다. 교수 이름 바로 뒤에 "강의·수업" 같은 말이 오면 연구실 얘기가 아니라고 보고 뺀다.
    """
    compact = re.sub(r"\s+", "", text)
    flat, longest = _alias_index()
    found: dict[str, int] = {}
    for i in range(len(compact)):
        for n in range(2, min(longest, len(compact) - i) + 1):
            key = compact[i:i + n]
            if key not in flat:
                continue
            if f"{key}\x00person" in flat:
                tail = compact[i + n: i + n + 8].removeprefix("님").replace("교수님", "").replace("교수", "")
                if any(tail.startswith(w) for w in _NOT_OFFICE_AFTER):
                    continue
            found[key] = max(found.get(key, 0), n)
    out: list[PlaceHit] = []
    seen: set[tuple[str, str, str]] = set()
    for key in sorted(found, key=lambda k: -len(k)):
        for hit in flat[key]:
            spot = (hit.building, hit.floor, _PAREN_TAIL.sub("", hit.label).replace(" ", ""))
            if spot not in seen:  # 같은 장소가 호실 목록과 시설 목록에 따로 있어도 한 번만
                seen.add(spot)
                out.append(hit)
            if len(out) >= limit:
                return out
    return out


def building_floor_info(names: list[str]) -> list[tuple[str, str]]:
    """(건물 이름, 있는 층 요약) — 에이전트에게 주는 학교 건물 목록."""
    return [(n, floors_text(n) if has_data(n) else "") for n in names]
