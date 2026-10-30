# 캠퍼스팟 backend

**「불편이 문제가 되기 전에」 — AI 기반 캠퍼스 문제 탐지·예측·해결 서비스 캠퍼스팟**의 API 서버(FastAPI)입니다. 신고 접수와 처리 워크플로우, 관리자 기능, 인증, 대화 기록을 담당하고 AI 판단은 별도 서비스인 [ai](https://github.com/CampuSpot-SKU/ai)에 요청합니다.

- 서비스 주소: https://campuspot-frontend-890230516680.asia-northeast3.run.app (관리자는 `/admin`)
- 전체 레포: https://github.com/CampuSpot-SKU (backend · ai · frontend)

## 하는 일
- **챗봇 신고/문의 처리**: 대화를 ai 서비스에 넘겨 의도 분류(신고/문의/잡담)와 신고 슬롯 채우기를 하고, 확정된 신고를 접수합니다. 사진 첨부, 개인정보(학번·전화번호) 마스킹 포함.
- **신고 워크플로우**: 접수 → 분류 → 배정 → 처리 → 해결. 우선순위 매트릭스, 처리 이력, 유사 사례 검색.
- **SLA·에스컬레이션**: 단계별(50/100/150%) 점검 배치로 우선순위를 올리고 담당자에게 메일 알림.
- **반복 패턴 탐지·재발 예측**: ai 서비스의 탐지·예측 결과를 문제 후보·예방 점검으로 제공.
- **관리자 API**: 접수 목록·상세, 상태 변경, 통계, 설정(카테고리·SLA·탐지 기준·조건-액션 규칙), 문의 로그, 신고 대화 열람.
- **인증**: 로그인 없이 사용 가능한 익명 세션 + 선택형 학생 로그인(로그인하면 이전 대화 조회), 관리자 로그인.

## 구조
```
app/
  routers/      API 엔드포인트 (chat, reports, admin, stats, detection, cron, user_auth ...)
  services/     업무 로직 (report_workflow, sla_escalation, report_agent, ai_client ...)
  models/       SQLAlchemy 모델
  schemas/      요청·응답 스키마
  middleware/   요청 ID 등 공통 처리
alembic/        DB 마이그레이션
scripts/        데모 데이터·관리자 계정 관리·에이전트 평가 스크립트
tests/          pytest
docs/           api-spec.md · erd.md · runbook.md · 설계 결정 기록(adr)
```

## 기술 스택
FastAPI · SQLAlchemy · Alembic · PostgreSQL(Supabase, pgvector) · Google Cloud Run · Cloud Scheduler · GitHub Actions · Sentry

## 배포
`main`에 push하면 GitHub Actions(`CI/CD backend`)가 ruff · mypy · pytest를 통과시킨 뒤 Google Cloud Run에 배포합니다. 배치(SLA 점검 등)는 Cloud Scheduler가 호출합니다. 데모 데이터 투입, 관리자 계정 관리, 에이전트 평가는 수동 실행 워크플로우(`seed-demo`, `manage-admins`, `agent-eval`)로 제공합니다.

API 명세는 [docs/api-spec.md](docs/api-spec.md), DB 구조는 [docs/erd.md](docs/erd.md)를 참고하세요.
