"""학번·전화번호 마스킹 — 저장·Gemini 전송 전에 `***`로 지운다 (작업 1-23, 명세서 11장 "개인정보 처리").

적용은 2겹:
  1) 입구: routers/chat.py `send_message()`가 받은 직후(사용자 문장·폼 값) → DB 저장·분류·판정이 전부 마스킹된 값만 봄
  2) Gemini 경계: services/ai_client.py가 ai 서비스로 보내는 문자열 → 입구를 우회한 값(옛 DB 메시지 등)도 새지 않게

패턴(9/30 확정, 명세 11장): 휴대전화(+82 포함)·일반전화(02·03x~06x), `학번` 뒤 숫자 6~10자리(숫자만),
앞뒤가 숫자가 아닌 연속 숫자 8~10자리. 호수(301호)·층·접수번호·연월일·시각은 건드리지 않는다.
알려진 한계: 붙여 쓴 날짜(20260930)도 8자리라 `***`가 됨 — 과잉 마스킹이 안전한 쪽이라 그대로 둠.
"""
import re

MASK = "***"

# 국내 휴대전화(+82 포함)와 일반전화. 앞뒤가 숫자면(더 긴 숫자의 일부) 제외
_PHONE = re.compile(
    r"(?<!\d)(?:\+82[\s.-]?1[016-9]|01[016-9]|02|0[3-6][1-5])[\s.-]?\d{3,4}[\s.-]?\d{4}(?!\d)"
)
# `학번` + (조사·기호) + 숫자 6~10자리 → 숫자만 가림
_STUDENT_KEYWORD = re.compile(r"(학번\s*(?:은|는|이|가|:|-|=)?\s*)\d{6,10}(?!\d)")
# 키워드 없이 연속 숫자 8~10자리 (학번 형식)
_LONG_DIGITS = re.compile(r"(?<!\d)\d{8,10}(?!\d)")


def mask_pii(text: str) -> str:
    """학번·전화번호를 `***`로. 여러 번 적용해도 결과가 같다(멱등). 순서: 전화 → 학번(키워드) → 긴 숫자."""
    if not text:
        return text
    text = _PHONE.sub(MASK, text)
    text = _STUDENT_KEYWORD.sub(lambda m: m.group(1) + MASK, text)
    return _LONG_DIGITS.sub(MASK, text)


def mask_optional(text: str | None) -> str | None:
    return None if text is None else mask_pii(text)
