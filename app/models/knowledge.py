"""RAG용 원본 문서(학칙·공지)와 임베딩 (pgvector).

실제로 읽고 쓰는 건 ai 서비스지만, 스키마(마이그레이션)는 backend 한 곳에서만 관리한다.
"""
import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models._common import pg_enum, uuid_pk
from app.models.enums import DocType

# Gemini 임베딩 출력 차원 (2026-09-28 결정: 768 — HNSW 인덱스 가능, 저장공간·속도 유리)
EMBEDDING_DIM = 768


class AdminRegDocument(Base):
    __tablename__ = "admin_reg_documents"

    id: Mapped[uuid.UUID] = uuid_pk()
    title: Mapped[str] = mapped_column(Text, nullable=False)
    doc_type: Mapped[DocType] = mapped_column(pg_enum(DocType, "doc_type"), nullable=False)
    article_no: Mapped[str | None] = mapped_column(String(50))  # 학칙 조항 번호, 공지는 null
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 공지 크롤러 중복 수집 방지 기준 (학칙은 null 가능 — null끼리는 중복 허용)
    source_url: Mapped[str | None] = mapped_column(Text, unique=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AdminFaqEmbedding(Base):
    __tablename__ = "admin_faq_embeddings"
    __table_args__ = (
        Index(
            "ix_admin_faq_embeddings_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("admin_reg_documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)
    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)  # 청킹은 조항 단위 유지
