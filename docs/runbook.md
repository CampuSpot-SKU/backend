# 장애 대응 런북

> 누가 문제를 마주치든 **같은 순서로** 원인을 좁혀가기 위한 체크리스트 (명세서 10-6).
> 로컬 실행 없이 GitHub Actions + 배포된 사이트 + 각 콘솔 화면만으로 확인하는 걸 전제로 함.
> 새로운 장애를 겪으면 맨 아래 "겪었던 문제" 표에 한 줄 추가하기.

## 0. 주소 모음

| 무엇 | 주소 |
|---|---|
| ai 서비스 | https://campuspot-ai-890230516680.asia-northeast3.run.app (`/healthz`, `/docs`) |
| backend 서비스 | GCP Console → Cloud Run → `campuspot-backend` 에서 URL 확인 (`/healthz`, `/docs`) |
| frontend | GCP Console → Cloud Run → `campuspot-frontend` |
| GitHub Actions | 각 레포(`CampuSpot-SKU/backend`·`ai`·`frontend`) → Actions 탭 |
| DB | Supabase 대시보드 → Table Editor / Logs |
| Gemini 사용량·결제 | https://ai.studio/projects |

## 1. 어디서 문제가 났는지 먼저 구분

### A. GitHub Actions가 빨간색
실행 화면에서 **어느 박스(test / build / deploy)의 어느 단계**가 빨간지부터 확인.

| 빨간 단계 | 의미 | 할 일 |
|---|---|---|
| test › Secret scan (gitleaks) | 커밋에 API 키·비밀번호 패턴이 들어감 | 로그에서 파일·줄 확인 → 값 제거 후 다시 커밋. **이미 push된 키는 유출된 걸로 보고 새로 발급** |
| test › Lint / Type check / tests | 코드 스타일·타입·테스트 실패 | 로그의 파일명:줄번호 오류를 Claude에게 그대로 붙여넣기 |
| build | Docker 이미지 빌드 실패 (의존성 설치 등) | 로그 마지막 20줄 확인. 대부분 requirements/package 버전 문제 |
| deploy › Run DB migrations | 마이그레이션 실패 → **배포 자체가 멈춘 상태(기존 버전은 정상 운영 중)** | 로그의 에러 확인. DB 접속 문제면 `DATABASE_URL` 시크릿, SQL 문제면 마이그레이션 파일 수정 |
| deploy › Deploy to Cloud Run | 컨테이너가 안 뜸 | GCP Console → Cloud Run → 해당 서비스 → **로그** 탭에서 시작 에러 확인 |

> test가 빨간색이면 deploy는 실행되지 않으므로 배포된 사이트는 **이전 버전 그대로**다.

### B. Actions는 초록인데 사이트가 이상함
1. **`/healthz` 확인** — backend는 `{"status":"ok","db":"ok"}`가 정상. `"db":"error"`면 DB 연결 문제(→ Supabase 상태 확인)
2. **ai 서비스 `/healthz`도 확인** — backend 문제로 보이는 것 중 상당수가 실제로는 ai 서비스 쪽 문제
3. **에러 응답 코드로 구분**

| 코드 | 흔한 원인 |
|---|---|
| 401 (ai 서비스) | `X-Internal-Secret` 불일치 — backend와 ai의 `AI_SERVICE_SECRET` 값이 다름 |
| 422 | 요청 형식 오류 (필드 이름·타입·빈 값). 응답 body의 `loc`이 틀린 필드 위치 |
| 500 | 코드 예외 — Cloud Run 로그에서 스택트레이스 확인 |
| 503 "잠시 후 다시 시도해주세요" (ai) | Gemini 호출이 재시도 후에도 실패. **Cloud Run 로그에서 `intent classify attempt ... failed` 줄의 원인 확인** (크레딧 소진·키 오류·모델명 오류 등) |

4. **로그 추적** — GCP Console → Cloud Run → 서비스 → 로그. backend 로그는 줄마다 `[요청ID]`가 찍히고, 응답 헤더 `X-Request-ID`와 같은 값이라 그 ID로 검색하면 한 요청의 흐름만 모아볼 수 있음
5. **외부 서비스 상태** — Gemini(AI Studio 사용량·결제), Supabase 대시보드. 우리 코드 문제가 아니면 코드를 고치지 말 것 — 이 판단이 늦으면 엉뚱한 코드만 계속 고치게 됨

## 2. 복구 방법

| 원인 | 복구 |
|---|---|
| 방금 배포한 코드 | **Cloud Run 직전 리비전으로 롤백**: GCP Console → Cloud Run → 서비스 → **리비전** 탭 → 직전 리비전 선택 → **트래픽 관리**에서 100% 지정. 몇 초 안에 복구됨. 그다음 코드를 고쳐서 다시 push |
| DB 마이그레이션 | 로컬에서 downgrade를 돌리지 않는 구조라 **되돌리는 새 마이그레이션을 추가해서 push**(forward-fix)가 기본. 데이터가 걸린 변경이면 마이그레이션 전에 Supabase 백업부터 |
| Gemini 크레딧·쿼터 | AI Studio에서 결제·한도 확인. 코드로 해결 안 됨 |
| 특정 기능만 문제 | 기능 킬스위치 환경변수(`ENABLE_DETECTION` 등)로 끄기 — **(아직 미구현, 기능 구현 시 추가 예정)** |

## 3. 겪었던 문제 (새로 겪으면 한 줄씩 추가)

| 날짜 | 증상 | 원인 | 해결 |
|---|---|---|---|
| 2026-09-28 | 배포 중 `Run DB migrations`에서 `No module named 'psycopg'` | `DATABASE_URL` 시크릿이 `postgresql+psycopg://`(psycopg3) 형식인데 설치된 드라이버는 psycopg2 | backend `config.py`·ai `db.py`에서 URL 형식을 psycopg2로 자동 통일 |
| 2026-09-28 | ai `/api/v1/intent/classify`가 503 | Gemini API 선불 크레딧 소진 (`402 RESOURCE_EXHAUSTED`) | AI Studio에서 크레딧 충전 (팀 결정 대기) |
| 2026-09-28 | Actions에 Node.js 20 deprecated 경고 | GitHub 공식 액션 구버전 | 액션 버전 업(checkout@v5 등), 러너 `ubuntu-24.04` 고정 |

## 아직 없는 것 (구현되면 이 문서에 반영)
- Sentry 연동 (에러 자동 수집) — 명세서엔 있지만 미구현
- ai 서비스 로그의 요청ID(correlation ID) — 현재 backend만 있음
- 기능 킬스위치
