"""인사·잡담·범위 밖 말에 대한 짧은 답 — Gemini를 다시 부르지 않고 미리 만든 문장 풀에서 고른다.

이유: 대화가 쓸데없이 이어지지 않게, 토큰을 쓰지 않게, 그러면서 매번 똑같은 문장만 나오지 않게.
- 같은 세션에서 최근에 쓴 문장은 피해서 고른다 (풀 안에서 돌려씀)
- 범위 밖을 또 말하면(최근에 범위 밖 안내를 이미 했으면) 더 짧고 단호한 문장 풀을 쓴다
- 모든 문장은 존댓말이고, 끝에 "할 수 있는 일"(시설 신고·행정 문의)로 돌려놓는다
분류(greeting/thanks/bye/smalltalk/about/off_topic)는 ai 서비스가 한다 (intent_classification.md의 talk).
"""
import random
from collections.abc import Sequence

POOLS: dict[str, tuple[str, ...]] = {
    "greeting": (
        "안녕하세요! 고장이나 불편한 곳, 학교 행정이 궁금하면 편하게 말씀해 주세요.",
        "반가워요! 캠퍼스 불편 신고나 학교 행정 문의를 도와드려요.",
        "안녕하세요, 캠퍼스팟이에요. 무엇을 도와드릴까요?",
        "어서 오세요! 시설 고장 신고나 행정 문의가 필요하면 말씀해 주세요.",
    ),
    "thanks": (
        "도움이 됐다니 기뻐요. 더 필요한 게 있으면 말씀해 주세요.",
        "천만에요! 또 불편한 점이 있으면 언제든 알려 주세요.",
        "별말씀을요. 필요할 때 다시 불러 주세요.",
    ),
    "bye": (
        "네, 안녕히 가세요! 또 필요하면 언제든 와 주세요.",
        "좋은 하루 보내세요. 불편한 점이 생기면 다시 말씀해 주세요.",
        "다음에 또 만나요. 수고하셨어요!",
    ),
    "smalltalk": (
        "그렇군요! 도와드릴 일이 있으면 말씀해 주세요.",
        "좋네요. 캠퍼스에서 불편한 점이 있으면 알려 주세요.",
        "네, 그렇죠. 시설 신고나 행정 문의는 언제든 말씀해 주세요.",
        "그러셨군요. 필요한 일이 생기면 편하게 말씀해 주세요.",
    ),
    "about": (
        "저는 캠퍼스 시설의 고장·불편을 접수하고, 학칙·공지를 바탕으로 학교 행정 문의에 답해 드리는 캠퍼스팟이에요.",
        "캠퍼스팟은 시설 신고 접수와 학교 행정 문의 답변을 도와드려요. "
        "예를 들면 '3층 화장실 물이 새요', '휴학 신청은 어떻게 해요?' 같은 말씀이에요.",
    ),
    "off_topic": (
        "그 부분은 제가 도와드리기 어려워요. 캠퍼스 시설 불편 신고나 학교 행정 문의라면 도와드릴 수 있어요.",
        "죄송하지만 그건 제 역할이 아니에요. 시설 고장 신고와 행정 문의만 도와드려요.",
        "그 질문은 답변드릴 수 없어요. 캠퍼스 불편 신고나 학교 행정 궁금증이 있으시면 말씀해 주세요.",
        "제가 다루는 범위 밖이에요. 고장·불편 신고와 학교 행정 문의라면 바로 도와드릴게요.",
    ),
}
# 범위 밖 안내를 이미 한 뒤 또 범위 밖 말을 할 때 — 더 짧고 단호하게
OFF_TOPIC_REPEAT: tuple[str, ...] = (
    "그것도 제 범위 밖이에요. 시설 신고나 행정 문의만 가능해요.",
    "이 주제는 도와드릴 수 없어요. 시설 신고·행정 문의만 받고 있어요.",
    "죄송해요, 그건 어려워요. 신고나 행정 문의만 도와드려요.",
)
RECENT_WINDOW = 6  # 최근 몇 개 챗봇 메시지까지 "이미 쓴 문장"으로 볼지


def _all_replies() -> set[str]:
    return {t for pool in POOLS.values() for t in pool} | set(OFF_TOPIC_REPEAT)


def pick_reply(
    talk: str, recent_assistant: Sequence[str], rng: random.Random | None = None
) -> str:
    """talk 종류에 맞는 답 한 문장. recent_assistant: 이 세션의 최근 챗봇 메시지(오래된 순)."""
    rng = rng or random.Random()
    recent = list(recent_assistant)[-RECENT_WINDOW:]
    pool = POOLS.get(talk, POOLS["smalltalk"])
    if talk == "off_topic" and any(t in recent for t in (*POOLS["off_topic"], *OFF_TOPIC_REPEAT)):
        pool = OFF_TOPIC_REPEAT
    fresh = [t for t in pool if t not in recent]
    return rng.choice(fresh or list(pool))
