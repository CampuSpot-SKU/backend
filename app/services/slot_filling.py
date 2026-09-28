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
같은 질문은 한 번만 한다 — 두 번째에도 못 알아들으면 있는 정보로 접수 (무한 되묻기 방지).
"""
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.models.enums import ChatIntent, ChatRole, Level

# ── 챗봇 문구 ────────────────────────────────────────────────────────────────
CANCEL_HINT = "\n(신고를 그만두려면 '취소'라고 입력해 주세요)"
ASK_LOCATION = "어디에서 생긴 문제인가요? 건물·층·장소를 알려주세요. (예: 3동 2층 화장실)"
ASK_PROBLEM = "어떤 문제인지 조금 더 자세히 알려주시겠어요? (예: 물이 새요, 불이 안 켜져요)"
CANCELLED = "신고 접수를 취소했어요. 다른 도움이 필요하면 편하게 말씀해 주세요."
CANCEL_WORDS = ("취소", "그만", "안 할래", "안할래")

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
LANDMARKS = ("정문", "후문", "기숙사", "도서관", "운동장", "학생회관", "본관", "체육관", "대강당")

# 한글 뒤에 조사가 붙어도 단어 끝으로 인정 ("공학관에서", "3동 2층")
_END = r"(?=$|[\s,.!?~]|에|의|은|는|이|가|을|를|도|쪽|앞|뒤|옆|안|내|로|서|까지)"
_BUILDING_NUM_RE = re.compile(r"(\d{1,3})\s*동" + _END)
_BUILDING_SUFFIX_RE = re.compile(r"([가-힣A-Za-z0-9]{1,10}(?:관|홀|센터))" + _END)
_FLOOR_RE = re.compile(r"(?:지하\s*(\d{1,2})\s*층|[Bb]\s*(\d{1,2})\s*층?|(\d{1,2})\s*층)")
_ROOM_RE = re.compile(r"(\d{3,4})\s*호")


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
    category: str | None = None  # categories.name (못 정하면 None → 접수 시 "기타")
    has_problem: bool = False
    impact: Level = Level.LOW
    urgency: Level = Level.LOW

    @property
    def has_location(self) -> bool:
        return bool(self.building or self.floor or self.detail)

    @property
    def location_text(self) -> str | None:
        """사람이 읽을 위치 문자열 (예: "3동 2층 화장실")."""
        parts = [self.building]
        if self.floor:
            parts.append(f"지하 {self.floor[1:]}층" if self.floor.startswith("B") else f"{self.floor}층")
        parts.append(self.detail)
        text = " ".join(p for p in parts if p)
        return text or None


def _find_first(text: str, words: Sequence[str]) -> str | None:
    """words 중 text에 가장 먼저(왼쪽에) 나오는 단어."""
    hits = [(text.find(w), w) for w in words if w in text]
    return min(hits)[1] if hits else None


def _match_building(text: str, buildings: Sequence[BuildingRef]) -> tuple[uuid.UUID | None, str | None]:
    # 1) buildings 테이블 이름·별칭 (긴 이름부터 — "공학관"보다 "제2공학관" 우선)
    candidates = [(alias, b) for b in buildings for alias in (b.name, *b.aliases) if alias]
    for alias, b in sorted(candidates, key=lambda c: len(c[0]), reverse=True):
        if alias in text:
            return b.id, b.name
    # 2) 목록에 없어도 건물처럼 보이는 표현 → location_raw로 저장됨
    m = _BUILDING_NUM_RE.search(text)
    if m:
        return None, f"{m.group(1)}동"
    landmark = _find_first(text, LANDMARKS)
    if landmark:
        return None, landmark
    for m in _BUILDING_SUFFIX_RE.finditer(text):
        if m.group(1) not in DETAIL_PLACES:  # "현관"은 건물이 아니라 세부 장소
            return None, m.group(1)
    return None, None


def _match_floor(text: str) -> str | None:
    m = _FLOOR_RE.search(text)
    if not m:
        return None
    basement = m.group(1) or m.group(2)
    return f"B{basement}" if basement else m.group(3)


def _match_category(lowered: str) -> str | None:
    for name, words in CATEGORY_KEYWORDS:
        if any(w in lowered for w in words):
            return name
    return None


def extract_slots(
    text: str, buildings: Sequence[BuildingRef] = (), safety_concern: bool = False
) -> ReportSlots:
    """신고 문장(여러 메시지를 합친 것)에서 슬롯을 뽑는다.

    ⚠️ Gemini로 교체할 때 이 함수만 바꾸면 됨 — 시그니처와 ReportSlots 형식은 유지할 것.
    safety_concern: ai 의도분류가 "안전 위험"이라고 판단했는지 (긴급도 상향 힌트, 명세서 4-4).
    """
    lowered = text.lower()
    building_id, building = _match_building(text, buildings)
    room = _ROOM_RE.search(text)
    detail = _find_first(text, DETAIL_PLACES) or (f"{room.group(1)}호" if room else None)
    category = _match_category(lowered)

    urgent = safety_concern or category == "안전" or any(w in lowered for w in URGENT_WORDS)
    private = any(w in text for w in PRIVATE_PLACES)
    # 위치를 모르거나 개인 공간이면 "저", 그 외(교내 공용 공간)는 "고"
    public = not private and bool(building or detail)

    return ReportSlots(
        building_id=building_id,
        building=building,
        floor=_match_floor(text),
        detail=detail,
        category=category,
        has_problem=category is not None or any(w in lowered for w in PROBLEM_WORDS),
        impact=Level.HIGH if public else Level.LOW,
        urgency=Level.HIGH if urgent else Level.LOW,
    )


# ── 대화 흐름 ────────────────────────────────────────────────────────────────
class MessageLike(Protocol):
    """chat_messages 한 줄 중 여기서 필요한 필드 (테스트에선 간단한 객체로 대체 가능)."""

    role: ChatRole
    content: str
    intent: ChatIntent | None
    intent_scores: dict[str, Any] | None


@dataclass
class Draft:
    """지금 진행 중인 신고 대화 (대화 기록에서 재구성)."""

    user_texts: list[str] = field(default_factory=list)  # 신고 내용으로 쓸 사용자 메시지들 (오래된 순)
    asked: set[str] = field(default_factory=set)  # 이미 물어본 질문 (ASK_LOCATION / ASK_PROBLEM)
    safety_concern: bool = False
    in_progress: bool = False  # 직전 챗봇 메시지가 되묻기였나 → 이번 메시지는 그 답변

    @property
    def text(self) -> str:
        return "\n".join(self.user_texts)


def collect_draft(history: Sequence[MessageLike]) -> Draft:
    """세션 대화 기록(오래된 순)에서 진행 중인 신고를 모은다. 이번에 보낸 메시지는 포함하지 않음."""
    draft = Draft()
    i = len(history) - 1
    # 끝에서부터 intent=신고인 메시지 구간을 거슬러 올라감
    while i >= 0 and history[i].intent == ChatIntent.REPORT:
        msg = history[i]
        if msg.role == ChatRole.USER:
            draft.user_texts.insert(0, msg.content)
            if (msg.intent_scores or {}).get("safety_concern"):
                draft.safety_concern = True
        else:
            draft.asked.update(q for q in (ASK_LOCATION, ASK_PROBLEM) if msg.content.startswith(q))
        i -= 1
    draft.in_progress = bool(history) and (
        history[-1].intent == ChatIntent.REPORT and history[-1].role == ChatRole.ASSISTANT
    )

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
    return draft


def is_cancel(text: str) -> bool:
    return any(w in text for w in CANCEL_WORDS)


def next_question(slots: ReportSlots, asked: set[str]) -> str | None:
    """빠진 필수 슬롯에 대한 질문. None이면 접수해도 됨 (같은 질문은 한 번만)."""
    if not slots.has_location and ASK_LOCATION not in asked:
        return ASK_LOCATION
    if not slots.has_problem and ASK_PROBLEM not in asked:
        return ASK_PROBLEM
    return None
