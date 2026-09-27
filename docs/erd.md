# ERD (DB 스키마)

> 실제 스키마의 원본은 `app/models/`(ORM 모델)와 `alembic/versions/`(마이그레이션)다.
> 이 그림은 이해를 돕기 위한 요약이므로, 모델을 바꾸면 이 문서도 같이 고칠 것.
> GitHub에서 이 파일을 열면 아래 다이어그램이 그림으로 보인다.

- PK는 전부 UUID(`gen_random_uuid()`), 시간은 `timestamptz`
- 모든 테이블 RLS 켜짐 (Supabase REST API 외부 노출 차단, 마이그레이션 `0003`)
- 설정 테이블 초기값·관리자 계정 시드는 마이그레이션 `0002`

```mermaid
erDiagram
    chat_sessions ||--o{ chat_messages : "대화"
    chat_sessions |o--o{ reports : "신고한 세션"
    categories ||--o{ reports : "분류"
    buildings |o--o{ reports : "위치"
    reports ||--o{ report_status_history : "상태 이력"
    admins |o--o{ report_status_history : "변경자(null=시스템)"
    reports ||--o{ problem_cluster_reports : ""
    problem_clusters ||--o{ problem_cluster_reports : "N:M"
    categories ||--o{ problem_clusters : ""
    buildings |o--o{ problem_clusters : ""
    categories ||--o{ prediction_stats : ""
    buildings |o--o{ prediction_stats : ""
    admin_reg_documents ||--o{ admin_faq_embeddings : "청크"

    reports {
        uuid id PK
        bigint display_no UK "사용자 안내용 접수번호"
        uuid session_id FK
        uuid category_id FK
        enum priority "P1~P4"
        enum status "접수/배정/처리중/해결/종료"
        uuid building_id FK "매칭 실패 시 null"
        string floor
        string detail "세부위치"
        text location_raw "건물 매칭 실패 시 자유텍스트"
        text description
        text photo_url
        string assigned_dept
        timestamptz sla_deadline
        timestamptz created_at
        timestamptz updated_at
    }
    report_status_history {
        uuid id PK
        uuid report_id FK
        enum from_status "최초 접수 시 null"
        enum to_status
        text memo
        uuid changed_by FK
        timestamptz changed_at
    }
    chat_sessions {
        uuid id PK
        string user_identifier "익명 세션쿠키"
        timestamptz started_at
    }
    chat_messages {
        uuid id PK
        uuid session_id FK
        enum role "user/assistant"
        text content
        enum intent "신고/문의/애매함"
        jsonb intent_scores
        jsonb debug_payload "실패·저신뢰 시에만"
        timestamptz created_at
    }
    admins {
        uuid id PK
        string login_id UK
        string password_hash "bcrypt"
        string name
        string dept
        string role
    }
    categories {
        uuid id PK
        string name UK
        bool is_active
    }
    buildings {
        uuid id PK
        string name UK
        text_array aliases "대화 속 다른 이름"
    }
    priority_matrix_rules {
        uuid id PK
        enum impact "고/저"
        enum urgency "고/저"
        enum resulting_priority "P1~P4"
    }
    sla_config {
        uuid id PK
        enum priority UK
        int sla_hours
        text escalation_50pct_action
        text escalation_100pct_action
        text escalation_150pct_action
    }
    detection_config {
        uuid id PK
        int threshold_count "기본 3"
        int threshold_hours "기본 72"
    }
    problem_clusters {
        uuid id PK
        uuid building_id FK
        string detail
        uuid category_id FK
        timestamptz detected_at
        enum status "후보/승격/기각"
    }
    problem_cluster_reports {
        uuid cluster_id PK,FK
        uuid report_id PK,FK
    }
    prediction_stats {
        uuid id PK
        uuid building_id FK
        string detail
        uuid category_id FK
        float avg_recurrence_days
        timestamptz last_occurred_at
        timestamptz predicted_next_at
    }
    admin_reg_documents {
        uuid id PK
        text title
        enum doc_type "학칙/공지"
        string article_no "학칙 조항번호"
        text content
        text source_url UK "공지 중복수집 방지"
        timestamptz published_at
        timestamptz updated_at
    }
    admin_faq_embeddings {
        uuid id PK
        uuid document_id FK
        vector embedding "768차원, HNSW 코사인"
        text chunk_text
    }
```
