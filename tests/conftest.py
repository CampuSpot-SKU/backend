"""모든 테스트 공통 — 레이트 리밋 기록은 테스트마다 비움 (S-3).

세션 생성 IP 한도(기본 10/분)가 테스트 전체에 걸쳐 쌓이면 상관없는 테스트가 429로 깨지므로.
"""
from collections.abc import Iterator

import pytest

from app.rate_limit import limiter


@pytest.fixture(autouse=True)
def _reset_rate_limits() -> Iterator[None]:
    limiter.reset()
    yield
    limiter.reset()
