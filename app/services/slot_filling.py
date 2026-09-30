"""신고 슬롯필링 — 대화에서 위치·상황을 뽑고, 빠진 게 있으면 무엇을 되물을지 정한다 (작업 1-3).

DB·네트워크를 쓰지 않는 순수 함수만 모아둔 파일이라 pytest로 바로 검증 가능 (tests/test_slot_filling.py).

[설계 결정 — 2026-09-28, status.md "명세 변경 제안"에 기록]
1) 추출 방식: 지금은 규칙 기반(키워드·정규식). Gemini 크레딧이 생기면 `extract_slots()` 한 함수만
   ai 서비스 호출로 바꾸면 된다 — 반환 형식(ReportSlots)만 지키면 나머지 코드는 그대로.
2) 상태 저장: 별도 테이블/컬럼 없이, 매 요청마다 chat_messages에서 "진행 중인 신고 대화"를 다시 모은다.
   규칙: 신고 흐름에 속한 메시지(사용자 답변 + 챗봇 되묻기)는 intent=신고로 저장하고,
   접수 완료·취소 메시지는 intent 없이 저장해서 흐름의 끝을 표시한다.
   → 대화 끝에서부터 intent=신고인 메시지가 이어지는 구간 = 지금 진행 중인 신고.

필수 슬롯은 "위치"와 "상황(무슨 문제인지)" 두 개. 사진은 선택사항(명세서 11장)이라 묻지 않는다.
같은 질문은 한 번만 한다 — 두 번째에도 못 알아들으면 있는 정보로 요약 (무한 되묻기 방지).

[신고 흐름 개편 — 2026-09-30, 작업 1-3c, 명세서 4-1]
- 슬롯이 다 차도 바로 접수하지 않고 **요약을 보여준 뒤** 학생이 [접수]를 눌러야 접수한다.
  요약 메시지는 SUMMARY_PREFIX로 시작하게 저장하고, 대화를 재구성할 때 이걸로 "요약 확인 중"을 안다.
- 위치 처리 규칙(은주관·4층 없는 건물·일반 장소 이름·시설 이름)은 app/data/campus.json 기준.
  위치 되묻기는 종류와 관계없이 대화 한 번에 최대 1번 ("location" 키).
"""
import json
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from app.models.enums import ChatIntent, ChatRole, Level

# ── 챗봇 문구 ────────────────────────────────────────────────────────────────
ASK_LOCATION = "어디에서 생긴 문제인가요? 건물·층·장소를 알려주세요. (예: 혜인관 2층 화장실)"
# 강의실·복도처럼 어디에나 있는 장소 이름만 말했을 때 (건물이 빠짐)
ASK_LOCATION_GENERIC = "어느 건물 몇 층 {detail}인가요? (예: 은주1관 3층 {detail})"
# 학교에 없는 건물 이름을 말했을 때 — 없는 건물을 건물로 받지 않고 공식 목록을 보여주며 되물음
ASK_UNKNOWN_BUILDING = (
    "'{name}'은(는) 학교 건물 목록에 없어요. 어느 건물인가요? "
    "(대일관·문예관·본관·북악관·상승관·유담관·은주1관·은주2관·청운관·한림관·혜인관·수인관)"
)
EUNJU_CHOICES = ["은주1관", "은주2관", "잘 모르겠어요"]
ASK_EUNJU = "은주1관인가요, 은주2관인가요? 잘 모르시면 '잘 모르겠어요'라고 해주세요."
ASK_FLOOR4 = "{building}에는 4층 표기가 없어요. 직접 세어 보신 층이 맞나요? 아니라면 실제 층을 알려주세요."
ASK_PROBLEM = "어떤 문제인지 조금 더 자세히 알려주시겠어요? (예: 물이 새요, 불이 안 켜져요)"
CANCELLED = "신고 접수를 취소했어요. 다른 도움이 필요하면 편하게 말씀해 주세요."
CANCEL_WORDS = ("취소", "그만", "안 할래", "안할래")
# 대화형 신고 흐름 (명세 4-1): 접수 제안 → (응) → 필요한 정보 묻기 → 문장으로 확인 → 접수
OFFER_MARKER = "접수를 도와드릴까요?"
OFFER_CHOICES = ["네, 접수해 주세요", "아니요, 안내만 받을게요"]
INTRO = "필요한 정보를 물어볼게요. "
DECLINED = "알겠어요, 접수는 하지 않을게요. 궁금한 게 있으면 편하게 물어봐 주세요."
ASK_EDIT = "어느 부분을 고칠까요? 바뀐 내용을 편하게 말씀해 주세요."
SUMMARY_MARKER = "접수할까요?"
SUMMARY_CHOICES = ["네, 접수해 주세요", "내용을 고칠래요", "취소할게요"]
LOCATION_CHOICES = ["은주1관", "은주2관", "혜인관", "유담관", "상승관", "잘 모르겠어요"]
FLOOR4_CHOICES = ["네, 맞아요"]
# 저장하는 챗봇 메시지 종류 — Gemini가 말투를 바꿔도 종류는 chat_messages.debug_payload["kind"]로 알 수 있음
KIND_OFFER, KIND_LOCATION, KIND_PROBLEM, KIND_SUMMARY, KIND_EDIT = (
    "offer", "location", "problem", "summary", "edit"
)
DONE_PREFIX = "신고가 접수됐어요"
RESET_NOTE = "(새 대화를 시작했어요)"  # 페이지를 새로 열었을 때 진행 중이던 신고 흐름을 끝내는 표식
THANKS_REPLY = "도움이 됐다니 다행이에요! 다른 불편한 점이 있으면 언제든 말씀해 주세요."

# 되묻기 종류 → 저장된 챗봇 메시지에서 알아보는 표식 (대화 재구성용)
LOCATION_MARKERS = (
    ASK_LOCATION,
    "어느 건물 몇 층",
    "은주1관인가요, 은주2관인가요?",
    "에는 4층 표기가 없어요",
    "학교 건물 목록에 없어요",
)
ASK_LOC, ASK_PROB = "location", "problem"  # asked 집합에 들어가는 키

# ── 카테고리 키워드 (카테고리 이름은 categories 테이블 시드값과 같아야 함) ──────────
# 위에서부터 먼저 걸리는 카테고리로 정한다 → 구체적인 것(안전·전기·IT)을 일반적인 것(시설·설비)보다 위에.
CATEGORY_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("안전", ("미끄", "추락", "낙하", "떨어질", "붕괴", "무너", "균열", "금이 갔", "깨진 유리",
             "유리가 깨", "화재", "연기", "가스", "비상구", "소화기", "넘어질", "다칠", "위험")),
    ("전기", ("조명", "전등", "형광등", "불이 안", "불이 꺼", "불 꺼", "깜빡", "콘센트", "전기",
             "누전", "감전", "정전", "스위치", "차단기", "스파크")),
    ("IT·네트워크", ("와이파이", "wifi", "인터넷", "네트워크", "프로젝터", "빔", "컴퓨터", "pc",
                    "모니터", "프린터", "출결", "랜선", "전자칠판")),
    ("청소·위생", ("냄새", "악취", "쓰레기", "더러", "청소", "벌레", "바퀴", "곰팡이", "오물",
                  "토사물", "휴지가 없", "휴지 없")),
    ("시설·설비", ("고장", "물이 새", "새요", "샌다", "누수", "수도", "정수기", "에어컨", "냉방",
                  "난방", "히터", "엘리베이터", "승강기", "변기", "막혔", "막혀", "문이 안", "손잡이", "잠금",
                  "도어락", "의자", "책상", "벤치", "파손", "부서", "깨졌", "창문", "블라인드", "배수")),
]
DEFAULT_CATEGORY = "기타"

# 카테고리는 못 정했지만 "뭔가 문제가 있다"는 건 알 수 있는 표현
PROBLEM_WORDS = ("안 돼", "안돼", "안 되", "안되", "안 나와", "안나와", "안 켜", "안켜", "안 열",
                 "안 닫", "안 터", "안터", "망가", "이상해", "이상하", "문제", "불편", "작동",
                 "멈췄", "멈춰", "꺼져", "끊겨", "끊겨요", "없어요")

# 긴급도 "고" 판정용 (명세서 3-1: 안전위협 또는 급속 악화)
URGENT_WORDS = ("누전", "감전", "스파크", "불꽃", "화재", "불이 났", "연기", "가스", "타는 냄새",
                "미끄", "추락", "낙하", "떨어질", "붕괴", "무너", "갇혔", "갇혀", "침수", "물이 넘",
                "깨진 유리", "유리가 깨", "위험", "다쳤", "다칠", "부상", "안전")

# 영향도 판정용 (명세서 3-1: 높음=다수 이용 공용공간, 낮음=개인·소수 공간)
PRIVATE_PLACES = ("연구실", "사무실", "교수실", "호실", "내 방", "우리 방", "사물함", "개인")

# 건물 안 세부 장소 → reports.detail
DETAIL_PLACES = ("화장실", "강의실", "복도", "계단", "엘리베이터", "승강기", "로비", "휴게실",
                 "열람실", "실습실", "실험실", "연구실", "사무실", "교수실", "샤워실", "세탁실",
                 "흡연구역", "출입구", "입구", "주차장", "옥상", "식당", "매점", "카페", "라운지",
                 "사물함", "베란다", "현관")
# 건물명 목록(buildings)이 비어 있어도 알아볼 수 있는 교내 장소
UNKNOWN_LANDMARKS = ("기숙사", "도서관", "운동장", "학생회관", "대강당")  # 학교 건물 목록에 없는 곳

# 한글 뒤에 조사가 붙어도 단어 끝으로 인정 ("공학관에서", "3동 2층")
_END = r"(?=$|[\s,.!?~]|에|의|은|는|이|가|을|를|도|쪽|앞|뒤|옆|안|내|로|서|까지)"
_BUILDING_NUM_RE = re.compile(r"(\d{1,3})\s*동" + _END)
_BUILDING_SUFFIX_RE = re.compile(r"([가-힣A-Za-z0-9]{1,10}(?:관|홀|센터))" + _END)
_FLOOR_RE = re.compile(r"(?:지하\s*(\d{1,2})\s*층|[Bb]\s*(\d{1,2})\s*층?|(\d{1,2})\s*층)")
_ROOM_RE = re.compile(r"(\d{3,4})\s*호")


# ── 캠퍼스 데이터 (app/data/campus.json — 다른 학교에 적용할 땐 이 파일을 교체) ──────────────
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_EUNJU_NUM_RE = re.compile(r"은주\s*([12])\s*관")
_GENDER_TOILET_RE = re.compile(r"(남|여)(?:자|성)?\s*화장실")
UNKNOWN_WORDS = ("모르", "몰라", "기억 안", "기억안")
EUNJU_UNSURE_SUFFIX = "(1·2관 미확정)"


@lru_cache(maxsize=1)
def campus_rules() -> dict[str, Any]:
    data: dict[str, Any] = json.loads((DATA_DIR / "campus.json").read_text(encoding="utf-8"))
    return data


# ── 추출 ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class BuildingRef:
    """buildings 테이블 한 줄 (이름·별칭으로 매칭)."""

    id: uuid.UUID
    name: str
    aliases: Sequence[str] = ()


@dataclass
class ReportSlots:
    """대화에서 뽑아낸 신고 정보. 모르는 값은 None."""

    building_id: uuid.UUID | None = None
    building: str | None = None  # 매칭된 건물명, 또는 매칭 실패 시 문장에서 찾은 건물 표현
    floor: str | None = None  # "2", "B1"
    detail: str | None = None  # "화장실", "301호"
    # detail이 호수·특정 시설처럼 위치를 특정하는 값인가 (강의실·복도 같은 일반 장소 이름이면 False)
    detail_specific: bool = False
    category: str | None = None  # categories.name (못 정하면 None → 접수 시 "기타")
    has_problem: bool = False
    impact: Level = Level.LOW
    urgency: Level = Level.LOW
    # 위치 되묻기 사유 — 은주관인데 1관/2관을 모름 / 4층 없는 건물에서 4층이라고 함
    ambiguous_building: str | None = None
    floor_check: bool = False
    # 학교 건물 목록에 없는 이름("3동", "공학관")을 말한 경우 — 건물로 받지 않고 되물음 (building은 비움)
    unknown_place: str | None = None

    @property
    def has_location(self) -> bool:
        """건물이 있거나 호수·특정 시설이 있어야 위치가 채워진 것 (층만, 일반 장소 이름만은 아님).

        없는 건물 이름을 말했으면 호수가 있어도 어느 건물인지 모르므로 채워진 것으로 보지 않음.
        """
        return bool(self.building) or (self.detail_specific and not self.unknown_place)

    @property
    def location_text(self) -> str | None:
        """사람이 읽을 위치 문자열 (예: "3동 2층 화장실")."""
        parts = [self.building or self.unknown_place]  # 없는 건물 이름도 관리자가 볼 수 있게 글자로는 남김
        if self.floor:
            parts.append(f"지하 {self.floor[1:]}층" if self.floor.startswith("B") else f"{self.floor}층")
        parts.append(self.detail)
        text = " ".join(p for p in parts if p)
        return text or None


def _find_first(text: str, words: Sequence[str]) -> str | None:
    """words 중 text에 가장 먼저(왼쪽에) 나오는 단어."""
    hits = [(text.find(w), w) for w in words if w in text]
    return min(hits)[1] if hits else None


def _building_ref(name: str, buildings: Sequence[BuildingRef]) -> tuple[uuid.UUID | None, str]:
    for b in buildings:
        if name == b.name or name in b.aliases:
            return b.id, b.name
    return None, name


def _resolve_eunju(text: str) -> tuple[str | None, bool]:
    """은주관 처리 → (확정된 건물명, 1관/2관 미확정 여부).

    "은주1관"·"은주 2관"이면 그 건물(여러 번 나오면 마지막). "은주관"만 있으면, 그 뒤에 "1관"/"2관"
    (되묻기에 대한 답)이 있는지 보고, 없으면 미확정.
    """
    nums = _EUNJU_NUM_RE.findall(text)
    if nums:
        return f"은주{nums[-1]}관", False
    pos = text.find("은주관")
    if pos < 0:
        return None, False
    answer = re.findall(r"(?<!\d)([12])\s*관", text[pos + 3:])
    if answer:
        return f"은주{answer[-1]}관", False
    return None, True


def _match_building(
    text: str, buildings: Sequence[BuildingRef]
) -> tuple[uuid.UUID | None, str | None, bool, str | None]:
    """(building_id, 건물 이름, 은주관 미확정 여부, 목록에 없는 건물 이름).

    건물로 인정하는 건 공식 목록(buildings 테이블, 없으면 campus.json의 12개)뿐이다. "3동"처럼
    목록에 없는 이름은 건물로 받지 않고 네 번째 값(unknown)으로만 돌려줘서 되묻게 한다.
    """
    eunju, unsure = _resolve_eunju(text)
    if eunju:
        bid, name = _building_ref(eunju, buildings)
        return bid, name, False, None
    if unsure:  # 건물은 비우고 사람이 읽을 표현만 남김 (관리자가 location_raw로 확인)
        return None, "은주관" + EUNJU_UNSURE_SUFFIX, True, None
    # 1) buildings 테이블 이름·별칭 (긴 이름부터 — 별칭이 이름보다 길 수 있음)
    candidates = [(alias, b) for b in buildings for alias in (b.name, *b.aliases) if alias]
    for alias, b in sorted(candidates, key=lambda c: len(c[0]), reverse=True):
        if alias in text:
            return b.id, b.name, False, None
    # 2) 테이블이 비어 있어도 공식 12개 이름은 알아봄 (id 없음 → location_raw)
    for name in sorted(campus_rules()["buildings"], key=len, reverse=True):
        if name in text:
            return None, name, False, None
    # 3) 건물 이름처럼 생겼지만 목록에 없는 표현 → 건물로 받지 않음
    known_places = set(campus_rules()["facilities"]) | set(campus_rules()["outdoor_places"])
    m = _BUILDING_NUM_RE.search(text)
    if m:
        return None, None, False, f"{m.group(1)}동"
    landmark = _find_first(text, UNKNOWN_LANDMARKS)
    if landmark:
        return None, None, False, landmark
    for m in _BUILDING_SUFFIX_RE.finditer(text):
        word = m.group(1)
        if word not in DETAIL_PLACES and word not in known_places:  # "현관"·"체육관"은 건물 이름이 아님
            return None, None, False, word
    return None, None, False, None


def _match_facility(text: str) -> tuple[str, str, str | None] | None:
    """스포렉스·카페 SP처럼 건물 안 시설 이름 → (시설 이름, 건물, 층). 긴 별칭 우선."""
    facilities: dict[str, dict[str, str]] = campus_rules()["facilities"]
    for alias in sorted(facilities, key=len, reverse=True):
        if alias in text:
            f = facilities[alias]
            return f["name"], f["building"], f["floor"] or None
    return None


def _match_floor(text: str) -> str | None:
    """층 — 여러 번 말했으면 마지막 (정정 반영: "4층이요" → "아니 3층이요")."""
    matches = _FLOOR_RE.findall(text)
    if not matches:
        return None
    g1, g2, g3 = matches[-1]
    basement = g1 or g2
    return f"B{basement}" if basement else g3


def _match_category(lowered: str) -> str | None:
    for name, words in CATEGORY_KEYWORDS:
        if any(w in lowered for w in words):
            return name
    return None


def _impact(text: str, building: str | None, detail: str | None) -> Level:
    """영향도: 개인 공간이거나 위치를 전혀 모르면 "저", 교내 공용 공간이면 "고"."""
    private = any(w in text for w in PRIVATE_PLACES)
    return Level.HIGH if (not private and bool(building or detail)) else Level.LOW


def extract_slots(
    text: str, buildings: Sequence[BuildingRef] = (), safety_concern: bool = False
) -> ReportSlots:
    """신고 문장(여러 메시지를 합친 것)에서 슬롯을 뽑는다.

    ⚠️ Gemini로 교체할 때 이 함수만 바꾸면 됨 — 시그니처와 ReportSlots 형식은 유지할 것.
    safety_concern: ai 의도분류가 "안전 위험"이라고 판단했는지 (긴급도 상향 힌트, 명세서 4-4).
    """
    lowered = text.lower()
    building_id, building, eunju_unsure, unknown_place = _match_building(text, buildings)
    facility = _match_facility(text)
    floor = _match_floor(text)
    if facility:
        fname, fbuilding, ffloor = facility
        if building is None and fbuilding:  # 시설 이름 → 건물 자동 채움
            building_id, building = _building_ref(fbuilding, buildings)
        floor = floor or ffloor

    room = _ROOM_RE.search(text)
    outdoor = _find_first(text, campus_rules()["outdoor_places"])  # 정문·서문 등 건물 밖 장소
    generic = _find_first(text, DETAIL_PLACES)
    gender = _GENDER_TOILET_RE.search(text)
    if generic == "화장실" and gender:  # 남/여는 학생이 말했을 때만 기록 (추측 금지)
        generic = f"{gender.group(1)}자 화장실"
    if facility:
        detail: str | None = fname if not generic or generic in fname else f"{fname} {generic}"
    elif room:
        detail = f"{room.group(1)}호" + (f" {generic}" if generic else "")
    elif outdoor and not building:
        detail = outdoor
    else:
        detail = generic
    category = _match_category(lowered)

    urgent = safety_concern or category == "안전" or any(w in lowered for w in URGENT_WORDS)
    return ReportSlots(
        building_id=building_id,
        building=building,
        floor=floor,
        detail=detail,
        detail_specific=bool(facility or room or (outdoor and not building)),
        category=category,
        has_problem=category is not None or any(w in lowered for w in PROBLEM_WORDS),
        impact=_impact(text, building or unknown_place, detail),
        urgency=Level.HIGH if urgent else Level.LOW,
        ambiguous_building=building if eunju_unsure else None,
        unknown_place=None if building else unknown_place,
        # 4층 표기가 없는 건물(campus.json)에서 4층이라고 하면 확인
        floor_check=bool(
            building and building in campus_rules()["no_4th_floor"] and floor == "4"
        ),
    )


def apply_form(
    slots: ReportSlots,
    building: str | None,
    floor: str | None,
    detail: str | None,
    buildings: Sequence[BuildingRef],
    text: str,
) -> ReportSlots:
    """접수 폼의 최종 값으로 위치를 덮어쓴다 ([접수] 누를 때). None이면 그 항목은 그대로 둠.

    폼에서 학생이 직접 고친 값이 우선이라, 빈 문자열은 "비움"으로 본다.
    """
    out = replace(slots, ambiguous_building=None, floor_check=False, unknown_place=None)
    if building is not None:
        name = building.strip()
        out.building_id, out.building = (None, None)
        if name:
            out.building_id, out.building = _building_ref(name, buildings)
    if floor is not None:
        f = floor.strip()
        out.floor = _match_floor(f if "층" in f else f + "층") or (f or None)
    if detail is not None:
        d = detail.strip()
        out.detail = d or None
        out.detail_specific = bool(d)
    out.impact = _impact(text, out.building, out.detail)
    return out


# ── 대화 흐름 ────────────────────────────────────────────────────────────────
class MessageLike(Protocol):
    """chat_messages 한 줄 중 여기서 필요한 필드 (테스트에선 간단한 객체로 대체 가능)."""

    role: ChatRole
    content: str
    intent: ChatIntent | None
    intent_scores: dict[str, Any] | None
    # (선택) 챗봇 메시지의 {"kind": ...} — 없으면 문구 표식으로 알아봄


@dataclass
class Draft:
    """지금 진행 중인 신고 대화 (대화 기록에서 재구성)."""

    # 슬롯(위치·상황) 추출에 쓸 사용자 메시지들 (오래된 순)
    user_texts: list[str] = field(default_factory=list)
    # 접수 "상황(description)"으로 쓸 메시지들 — 위치 되묻기·요약 정정에 대한 짧은 답("혜인관 3층이요")은
    # 위치 추출에만 쓰고 여기엔 안 넣음 (그 답에 문제 내용이 들어 있으면 넣음)
    desc_texts: list[str] = field(default_factory=list)
    asked: set[str] = field(default_factory=set)  # 이미 물어본 질문 종류 (ASK_LOC / ASK_PROB)
    safety_concern: bool = False
    in_progress: bool = False  # 직전 챗봇 메시지가 신고 흐름 안의 말(되묻기·요약)이었나
    confirming: bool = False  # 직전 챗봇 메시지가 요약 확인이었나 → 이번 메시지는 접수/수정/취소
    last_kind: str | None = None  # 직전 챗봇 메시지 종류: offer / location / problem / summary / edit

    @property
    def text(self) -> str:
        return "\n".join(self.user_texts)

    def with_reply(self, text: str) -> tuple[list[str], list[str]]:
        """이번 메시지를 반영한 (추출용, 상황용) 목록. 저장된 대화에 쓰는 규칙과 같음."""
        extract, desc = list(self.user_texts), list(self.desc_texts)
        _apply_reply(extract, desc, self.last_kind, text)
        return extract, desc


def is_question_like(text: str) -> bool:
    """되묻기에 대한 답이 아니라 학생이 다른 걸 묻는 말 ("3동이 우리학교에 있어?")."""
    return "?" in text or "？" in text


def _has_problem_text(text: str) -> bool:
    return _match_category(text.lower()) is not None or any(w in text for w in PROBLEM_WORDS)


def _apply_reply(extract: list[str], desc: list[str], prev_kind: str | None, text: str) -> None:
    """되묻기·요약 뒤에 온 사용자 메시지 하나를 추출용/상황용 목록에 반영."""
    if prev_kind in (KIND_LOCATION, KIND_PROBLEM, KIND_OFFER) and is_question_like(text):
        return  # 되묻기와 상관없는 질문 — 위치로도 상황으로도 쓰지 않음
    extract.append(text)
    if prev_kind == KIND_PROBLEM or prev_kind is None or _has_problem_text(text):
        desc.append(text)


def _message_kind(msg: MessageLike) -> str | None:
    payload = getattr(msg, "debug_payload", None)
    if isinstance(payload, dict) and payload.get("kind"):
        return str(payload["kind"])
    content = msg.content  # 이전 버전·테스트 메시지: 고정 문구 표식으로 알아봄
    if SUMMARY_MARKER in content:
        return KIND_SUMMARY
    if OFFER_MARKER in content:
        return KIND_OFFER
    if ASK_EDIT in content:
        return KIND_EDIT
    if any(m in content for m in LOCATION_MARKERS):
        return KIND_LOCATION
    if ASK_PROBLEM in content:
        return KIND_PROBLEM
    return None


def collect_draft(history: Sequence[MessageLike]) -> Draft:
    """세션 대화 기록(오래된 순)에서 진행 중인 신고를 모은다. 이번에 보낸 메시지는 포함하지 않음."""
    draft = Draft()
    i = len(history) - 1
    # 끝에서부터 intent=신고인 메시지 구간을 거슬러 올라감 (구간의 시작 위치를 찾음)
    while i >= 0 and history[i].intent == ChatIntent.REPORT:
        i -= 1
    prev_kind: str | None = None
    for msg in history[i + 1:]:
        if msg.role == ChatRole.USER:
            _apply_reply(draft.user_texts, draft.desc_texts, prev_kind, msg.content)
            if (msg.intent_scores or {}).get("safety_concern"):
                draft.safety_concern = True
            prev_kind = None
        else:
            prev_kind = _message_kind(msg)
            if prev_kind == KIND_LOCATION:
                draft.asked.add(ASK_LOC)
            if prev_kind == KIND_PROBLEM:
                draft.asked.add(ASK_PROB)
    last_is_report_reply = bool(history) and (
        history[-1].intent == ChatIntent.REPORT and history[-1].role == ChatRole.ASSISTANT
    )
    draft.in_progress = last_is_report_reply
    draft.last_kind = _message_kind(history[-1]) if last_is_report_reply else None
    draft.confirming = draft.last_kind == KIND_SUMMARY

    # "계단이 미끄러운데 어떻게 해요?" → (애매함, 되묻기) → "신고해주세요" 흐름이면
    # 되묻기 직전의 원래 문장도 신고 내용에 포함 (그래야 상황을 다시 묻지 않음)
    if (
        i >= 1
        and history[i].role == ChatRole.ASSISTANT
        and history[i].intent == ChatIntent.UNCLEAR
        and history[i - 1].role == ChatRole.USER
        and history[i - 1].intent == ChatIntent.UNCLEAR
    ):
        draft.user_texts.insert(0, history[i - 1].content)
        draft.desc_texts.insert(0, history[i - 1].content)
    return draft


def _compact(text: str) -> str:
    return re.sub(r"[\s.,!~?\[\]]+", "", text).lower()


def is_cancel(text: str) -> bool:
    return any(w in text for w in CANCEL_WORDS)


CONFIRM_WORDS = {"접수", "네", "넵", "넹", "예", "응", "웅", "좋아요", "좋아", "맞아요", "맞아",
                 "확인", "그래", "그래요", "ㅇㅇ", "ㅇㅋ", "오케이", "ok"}


def is_confirm(text: str) -> bool:
    """요약 확인 단계에서 "접수해 주세요"에 해당하는 말 (버튼이 없는 화면용)."""
    t = _compact(text)
    if t in CONFIRM_WORDS:
        return True
    t = re.sub(r"^(네|넵|넹|예|응|웅)", "", t)  # "네 접수해 주세요"
    return t.startswith("접수") and len(t) <= 10 and not any(w in t for w in ("안", "말", "취소"))


YES_HINTS = ("도와", "해줘", "해주세요", "부탁", "그렇게", "진행")
NO_WORDS = ("아니", "싫", "괜찮", "됐어", "안 할", "안할", "필요 없", "필요없")
EDIT_WORDS = ("고칠", "수정", "바꿀", "바꿔", "틀렸", "잘못", "다시")


def is_yes(text: str) -> bool:
    """접수 제안에 대한 긍정 ("응", "네, 접수해 주세요", "도와주세요")."""
    t = _compact(text)
    return is_confirm(text) or (
        len(t) <= 14 and any(w in t for w in YES_HINTS) and not is_no(text)
    )


def is_no(text: str) -> bool:
    return any(w in text for w in NO_WORDS)


def is_edit(text: str) -> bool:
    """요약 확인 단계에서 "내용을 고칠래요"에 해당하는 짧은 말 (고칠 내용이 같이 들어 있으면 그 내용은 정정으로 처리)."""
    t = _compact(text)
    if t == _compact(SUMMARY_CHOICES[1]):
        return True
    return len(t) <= 5 and (any(w in t for w in EDIT_WORDS) or t.startswith("아니"))


def wants_inquiry(text: str) -> bool:
    """신고 흐름 중 "안내만 받을래요"를 글자로 입력한 경우."""
    return "안내만" in text


THANKS_WORDS = ("감사", "고마", "고맙", "네", "넵", "넹", "알겠", "확인", "ㅇㅋ", "오케이", "수고")


def is_short_thanks(text: str) -> bool:
    """접수 완료 직후의 인사·확인성 짧은 답 (새 신고로 시작하면 안 됨 — 명세 4-1 보완 ②)."""
    t = text.strip()
    return len(t) <= 12 and any(w in t for w in THANKS_WORDS) and "?" not in t and "어떻게" not in t


@dataclass
class Question:
    text: str
    key: str  # ASK_LOC / ASK_PROB
    choices: list[str] | None = None  # 화면에 눌러서 고를 수 있는 추천 답변 (직접 입력도 가능)
    must_include: list[str] = field(default_factory=list)  # 말투를 바꿔도 남아 있어야 하는 표현


def next_question(slots: ReportSlots, asked: set[str]) -> Question | None:
    """빠진 필수 슬롯에 대한 질문. None이면 요약 단계로 가도 됨 (같은 종류 질문은 한 번만).

    위치 되묻기(은주관 / 4층 확인 / 위치 없음·일반 장소 이름만)는 어떤 종류든 합쳐서 1번.
    """
    if ASK_LOC not in asked:
        if slots.ambiguous_building:
            return Question(ASK_EUNJU, ASK_LOC, list(EUNJU_CHOICES), ["은주1관", "은주2관"])
        if slots.unknown_place and not slots.building:
            names: list[str] = list(campus_rules()["buildings"])
            return Question(
                ASK_UNKNOWN_BUILDING.format(name=slots.unknown_place), ASK_LOC,
                [*names[:5], "잘 모르겠어요"], names,
            )
        if slots.floor_check and slots.building:
            return Question(
                ASK_FLOOR4.format(building=slots.building), ASK_LOC, list(FLOOR4_CHOICES),
                [slots.building],
            )
        if not slots.has_location:
            if slots.detail:  # 강의실·복도처럼 일반 장소 이름만 → 건물을 물음
                return Question(
                    ASK_LOCATION_GENERIC.format(detail=slots.detail), ASK_LOC,
                    list(LOCATION_CHOICES),
                )
            return Question(ASK_LOCATION, ASK_LOC, list(LOCATION_CHOICES))
    if not slots.has_problem and ASK_PROB not in asked:
        return Question(ASK_PROBLEM, ASK_PROB)
    return None


# ── 메시지 문구 ──────────────────────────────────────────────────────────────
def _one_line(text: str, limit: int = 80) -> str:
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def build_offer(text: str) -> str:
    """접수 제안 기본 문구 (Gemini가 말투를 바꿀 수 있음 — 접수를 도와드릴까요? 뜻은 유지)."""
    return (
        f"시설물 신고 접수에 관한 내용 같아요. '{_one_line(text, 40)}' 건으로 "
        f"{OFFER_MARKER}"
    )


def build_summary(slots: ReportSlots, texts: Sequence[str]) -> str:
    """접수 직전 확인 문구 — 목록이 아니라 문장 하나. SUMMARY_MARKER를 포함해야 이전 버전 재구성에서도 인식됨."""
    situation = _one_line(" ".join(texts)) if slots.has_problem else "내용은 아직 확인이 안 됐어요"
    if slots.location_text and not (slots.unknown_place and not slots.building):
        body = f"{slots.location_text}에서 '{situation}' 문제예요."
    elif slots.unknown_place and not slots.building:
        body = (
            f"'{situation}' 문제이고, '{slots.unknown_place}'은(는) 학교 건물 목록에 없어서 "
            "위치는 담당자가 확인할게요."
        )
    else:
        body = f"'{situation}' 문제이고, 위치는 담당자가 확인할게요."
    return f"정리해 볼게요. {body} 이대로 {SUMMARY_MARKER}"


def judge_reason(slots: ReportSlots, priority: str) -> str:
    """접수 완료 메시지에 붙이는 판정 이유 한 줄 (명세 4-1). 지금은 규칙 기반 — 1-3b에서 AI 판정으로 교체."""
    impact = "여러 사람이 쓰는 공간이고" if slots.impact == Level.HIGH else "개인 공간이거나 위치가 불분명하고"
    urgency = "안전 위험 신호가 있어" if slots.urgency == Level.HIGH else "급한 위험 신호는 없어"
    return f"{impact} {urgency} {priority}로 판단했어요."
