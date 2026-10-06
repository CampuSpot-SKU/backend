"""DB ENUM 값 — 명세서 5장 기준. 값(value)이 그대로 DB에 저장된다.

워크플로우 단계(status)처럼 학교가 달라도 구조가 같은 값만 ENUM으로 둔다.
카테고리·우선순위 기준·SLA 시간처럼 학교마다 다를 수 있는 값은 ENUM이 아니라
설정 테이블(models/config.py)로 관리한다.
"""
import enum


class ReportStatus(str, enum.Enum):
    RECEIVED = "접수"
    ASSIGNED = "배정"
    IN_PROGRESS = "처리중"
    RESOLVED = "해결"
    CLOSED = "종료"


class Priority(str, enum.Enum):
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"


class Level(str, enum.Enum):
    """영향도/긴급도 (우선순위 매트릭스 축)"""

    HIGH = "고"
    LOW = "저"


class ChatRole(str, enum.Enum):
    USER = "user"
    ASSISTANT = "assistant"


class ChatIntent(str, enum.Enum):
    REPORT = "신고"
    INQUIRY = "문의"
    UNCLEAR = "애매함"


class DocType(str, enum.Enum):
    REGULATION = "학칙"
    NOTICE = "공지"
    GUIDE = "안내"


class ClusterStatus(str, enum.Enum):
    CANDIDATE = "후보"
    PROMOTED = "승격"
    REJECTED = "기각"
