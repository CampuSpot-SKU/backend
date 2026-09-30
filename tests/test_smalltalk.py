import random

from app.services.smalltalk import OFF_TOPIC_REPEAT, POOLS, pick_reply


def test_every_reply_is_polite_and_short() -> None:
    for text in [t for pool in POOLS.values() for t in pool] + list(OFF_TOPIC_REPEAT):
        assert len(text) <= 120
        assert text.rstrip("!.?").endswith(("요", "다", "죠", "게요"))  # 존댓말


def test_recent_replies_are_avoided() -> None:
    rng = random.Random(0)
    used: list[str] = []
    for _ in range(len(POOLS["greeting"])):
        used.append(pick_reply("greeting", used, rng))
    assert len(set(used)) == len(POOLS["greeting"])  # 풀이 다 돌 때까지 겹치지 않음


def test_second_off_topic_uses_terse_pool() -> None:
    first = pick_reply("off_topic", [])
    assert first in POOLS["off_topic"]
    assert pick_reply("off_topic", [first]) in OFF_TOPIC_REPEAT
    # 인사 뒤의 첫 범위 밖은 여전히 일반 안내
    assert pick_reply("off_topic", [POOLS["greeting"][0]]) in POOLS["off_topic"]


def test_unknown_talk_falls_back_to_smalltalk() -> None:
    assert pick_reply("whatever", []) in POOLS["smalltalk"]
