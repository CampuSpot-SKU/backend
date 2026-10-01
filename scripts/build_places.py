"""학교 장소 목록을 정규화된 데이터(app/data/places.json)로 만든다.

원본: location_options.json(건물·층·호실), campus.json(시설·야외·층 규칙), campus_geo.json(별칭·건물 간 위치관계).
새 호실·시설·별칭을 원본에 추가한 뒤 이 스크립트를 다시 돌려 places.json을 갱신한다.
  python scripts/build_places.py
places.json 한 줄 = 장소 하나: id, name, kind, building, floor, room_no, numbered, use, aliases[], near[], relation.
"""
import json
import re
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "app" / "data"
PAREN = re.compile(r"\s*\([^)]*\)\s*$")
PERSON = re.compile(r"^([가-힣]{2,4})\s*교수\s*연구실$")
GENERIC = {"강의실", "화장실", "복도", "계단", "엘리베이터", "사무실", "실습실", "실험실", "연구실", "교수실"}
# 호수 없는 공간(번호로 부르지 않음)과 건물 전체·공용 설비 — 에이전트가 "호수·층을 묻지 않는" 기준
SPACE_TYPES = [
    {"name": "화장실", "numbered": False, "building_wide": False, "room": True},
    {"name": "복도", "numbered": False, "building_wide": True, "room": False},
    {"name": "로비", "numbered": False, "building_wide": True, "room": False},
    {"name": "계단", "numbered": False, "building_wide": True, "room": False},
    {"name": "엘리베이터", "numbered": False, "building_wide": True, "room": False},
    {"name": "강의실", "numbered": True, "building_wide": False, "room": True},
    {"name": "연구실", "numbered": True, "building_wide": False, "room": True},
    {"name": "사무실", "numbered": True, "building_wide": False, "room": True},
    {"name": "실습실", "numbered": True, "building_wide": False, "room": True},
]


def person_aliases(name: str) -> list[str]:
    base = name if len(name) >= 3 else name + "교수"
    out = {base, f"{name}교수", f"{name}교수님", f"{name}교수실", f"{name}교수연구실", f"{name}교수님연구실", f"{name}교수님방"}
    return sorted(out)


def main() -> None:
    options = json.loads((DATA / "location_options.json").read_text(encoding="utf-8"))
    rules = json.loads((DATA / "campus.json").read_text(encoding="utf-8"))
    geo = json.loads((DATA / "campus_geo.json").read_text(encoding="utf-8"))
    places: list[dict] = []
    buildings = []
    for b in options["buildings"]:
        if not b.get("name") or b.get("custom"):
            continue
        floors = []
        for f in b.get("floors", []):
            if f.get("custom") or not f.get("floor"):
                continue
            floors.append(str(f["floor"]))
            for p in f.get("places", []):
                if p.get("custom"):
                    continue
                core = PAREN.sub("", p["label"]).strip()
                person = PERSON.match(core)
                aliases = person_aliases(person.group(1)) if person else []
                places.append({
                    "name": p["label"], "core": core, "kind": "person_office" if person else "room",
                    "building": b["name"], "floor": str(f["floor"]), "room_no": p.get("room_no"),
                    "numbered": bool(p.get("room_no")), "use": p.get("use"), "aliases": aliases,
                    "generic": core.replace(" ", "") in GENERIC or "화장실" in core,
                })
        buildings.append({"name": b["name"], "floors": floors,
                          "aliases": [], "no_4th_floor": b["name"] in rules.get("no_4th_floor", [])})
    for alias, f in rules.get("facilities", {}).items():
        places.append({"name": f["name"], "core": f["name"], "kind": "facility", "building": f["building"],
                       "floor": f.get("floor") or "", "room_no": None, "numbered": False, "use": None,
                       "aliases": [alias], "generic": False})
    for name in rules.get("outdoor_places", []):
        places.append({"name": name, "core": name, "kind": "outdoor", "building": "", "floor": "",
                       "room_no": None, "numbered": False, "use": None, "aliases": [name], "generic": False})
    for name, lm in geo.get("landmarks", {}).items():
        places.append({"name": lm["label"], "core": name, "kind": "landmark", "building": "", "floor": "",
                       "room_no": None, "numbered": False, "use": None, "aliases": [name],
                       "near": lm["buildings"], "relation": lm["relation"], "generic": False})
    for i, p in enumerate(places, 1):
        p["id"] = f"P{i:04d}"
    out = {"buildings": buildings, "space_types": SPACE_TYPES, "term_aliases": geo.get("term_aliases", {}),
           "places": places}
    (DATA / "places.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"buildings={len(buildings)} places={len(places)}")


if __name__ == "__main__":
    main()
