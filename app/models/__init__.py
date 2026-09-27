"""ORM 모델 모음 (명세서 5장 테이블 16개).

새 모델 파일을 만들면 반드시 여기서 import할 것 — Alembic(env.py)은 이 패키지를
import해서 Base.metadata에 등록된 테이블만 인식한다.
"""
from app.db.base import Base
from app.models.admin import Admin
from app.models.chat import ChatMessage, ChatSession
from app.models.config import (
    Building,
    Category,
    DetectionConfig,
    PriorityMatrixRule,
    SlaConfig,
)
from app.models.knowledge import AdminFaqEmbedding, AdminRegDocument
from app.models.problem import PredictionStat, ProblemCluster, problem_cluster_reports
from app.models.report import Report, ReportStatusHistory

__all__ = [
    "Admin",
    "AdminFaqEmbedding",
    "AdminRegDocument",
    "Base",
    "Building",
    "Category",
    "ChatMessage",
    "ChatSession",
    "DetectionConfig",
    "PredictionStat",
    "PriorityMatrixRule",
    "ProblemCluster",
    "Report",
    "ReportStatusHistory",
    "SlaConfig",
    "problem_cluster_reports",
]
