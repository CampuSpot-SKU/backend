# API 명세

API 문서는 별도로 손으로 관리하지 않고, **FastAPI가 코드(Pydantic 모델)에서 자동 생성하는
Swagger 화면**을 기준으로 한다 (코드와 문서가 어긋날 일이 없음).

| 서비스 | 자동 생성 문서 | OpenAPI JSON |
|---|---|---|
| backend | https://campuspot-backend-890230516680.asia-northeast3.run.app/docs | `/openapi.json` |
| ai (내부 전용) | https://campuspot-ai-890230516680.asia-northeast3.run.app/docs | `/openapi.json` |

## 규칙
- 모든 엔드포인트는 `/api/v1` 프리픽스 (헬스체크 `/health`만 예외)
- 외래키는 `_id`로 끝나는 필드명으로 주고받고, 사람이 읽을 이름이 필요하면 `category: {id, name}`처럼
  중첩 객체로 함께 응답
- 요청·응답 형식은 반드시 Pydantic 모델로 선언 — 그래야 위 `/docs`에 자동으로 나타남
- ai 서비스는 backend만 호출: 모든 요청에 `X-Internal-Secret` 헤더 필요 (없으면 401)
- 에러 형식: FastAPI 기본 `{"detail": ...}`. 외부 AI 호출 실패는 503 `"잠시 후 다시 시도해주세요"`

## 구현 현황
| 엔드포인트 | 상태 |
|---|---|
| backend `GET /health` | ✅ DB 연결 상태 포함 |
| ai `GET /health` | ✅ |
| ai `POST /api/v1/intent/classify` | ✅ 의도 분류 (신고/문의/애매함 + 점수) |
| backend `POST /api/v1/chat/sessions` | ✅ 세션 시작 (1-3) |
| backend `POST /api/v1/chat/sessions/{session_id}/messages` | ✅ 신고 슬롯필링·접수 생성, 애매함 되묻기, 세션당 요청 제한 (1-3). 행정문의는 SSE 형식만 맞춘 임시 답변 — 1-4에서 RAG 연결 |
| backend `POST /api/v1/admin/auth/login` | ✅ 관리자 로그인 → JWT (1-6). 배포 환경에 `JWT_SECRET` 필요 (없으면 503) |
| backend `GET /api/v1/admin/reports`, `GET /api/v1/admin/reports/{id}` | ✅ 접수 목록(필터·정렬·sla_status)·상세+상태 이력 (1-6) |
| backend `PATCH /api/v1/admin/reports/{id}/status` | ✅ 상태 변경 + 이력(변경 관리자·메모) (1-6). 전이 규칙은 1-7에서 `report_workflow.check_transition()`에 추가 |
| backend `/reports/*`, `/admin/problem-clusters`·`/admin/predictions`·`/admin/config/*`, `/cron/*` | 🚧 Phase 1 진행 중 |
| ai `POST /api/v1/rag/answer`, `/api/v1/cron/*` | 🚧 Phase 1 진행 중 |

엔드포인트를 구현하면 위 표의 상태를 갱신할 것.
