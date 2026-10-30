# CampuSpot backend

> 「불편이 문제가 되기 전에」 AI 기반 캠퍼스 문제 탐지·예측·해결 서비스 **캠퍼스팟(CampuSpot)**

캠퍼스 불편 신고와 학교 행정 문의를 하나의 챗봇으로 받는 서비스의 **API 서버**입니다.
챗봇 대화·신고 접수와 처리 흐름(접수→분류→배정→처리→해결)·관리자 화면 API·학생 계정을 맡고,
AI 판단(의도 분류, 신고 대화, 행정 문의 답변, 반복 패턴 탐지·재발 예측)은 `ai` 서비스를 호출해서 씁니다.

- 배포 주소: https://campuspot-frontend-890230516680.asia-northeast3.run.app (관리자 화면은 `/admin`)
- 전체 레포: https://github.com/CampuSpot-SKU

## 3개 레포가 연결되는 방식

```
[학생·관리자 브라우저]
        │
   frontend (React + TypeScript)        ← https://github.com/CampuSpot-SKU/frontend
        │  HTTPS /api/v1/...
   backend (FastAPI, 이 레포)            ← DB(PostgreSQL + pgvector)·사진 저장소 접근
        │  HTTP (서비스 간 내부 인증 헤더)
   ai (FastAPI + Gemini)                ← https://github.com/CampuSpot-SKU/ai
```

- frontend는 backend만 호출합니다. backend가 필요할 때 ai 서비스를 호출해 결과를 받아 DB에 저장하고 화면에 돌려줍니다.
- 세 서비스는 각각 별도의 Google Cloud Run 서비스로 배포됩니다.

## 주요 기능 (API 경로는 모두 `/api/v1` 아래)

| 구분 | 경로 | 내용 |
|---|---|---|
| 챗봇 | `/chat/...` | 대화 세션 생성·메시지 처리, 신고 사진 업로드, 이전 대화 조회(로그인 시) |
| 신고 조회 | `/reports/{접수번호}` | 학생이 접수번호로 처리 상태 확인 |
| 학생 계정 | `/auth/...` | 회원가입·로그인(선택 사항) |
| 관리자 | `/admin/...` | 접수 목록·상세·상태 변경, 신고 대화 열람, 통계, 문의 로그, 문제 후보·재발 예측, 설정(카테고리·우선순위 매트릭스·SLA·조건-액션 규칙) |
| 배치 | `/cron/...` | SLA 임박·초과 점검 |
| 상태 확인 | `/health` | 헬스체크 |

## 폴더 구조

```
app/
  main.py          FastAPI 진입점, 라우터 등록
  routers/         API 경로별 라우터 (chat, reports, admin, stats, detection, config_admin, user_auth, cron …)
  services/        업무 로직 (신고 워크플로우, 신고 대화 에이전트 연동, SLA 에스컬레이션, 개인정보 마스킹, 사진 저장 …)
  models/          SQLAlchemy 모델 (신고·대화·관리자·사용자·설정·문제 후보 …)
  schemas/         Pydantic 요청·응답 형식
  middleware/      공통 미들웨어
  db/              DB 연결
alembic/           DB 마이그레이션
scripts/           데모 데이터·관리자 계정 관리·에이전트 평가 스크립트
tests/             pytest 테스트
docs/              API 명세(api-spec.md)·ERD·운영 안내·설계 결정 기록(adr/)
```

## 기술 스택

- Python 3.11, FastAPI, Uvicorn
- SQLAlchemy 2, Alembic, PostgreSQL(Supabase) + pgvector
- Pydantic, PyJWT·bcrypt(로그인), slowapi(요청 횟수 제한), httpx(ai 서비스 호출), Sentry(에러 추적)
- 품질 검사: ruff, mypy, pytest, gitleaks

## 배포 방식

`main` 브랜치에 push → GitHub Actions(`.github/workflows/deploy.yml`) →
비밀값 검사·린트·타입 검사·테스트 → Docker 이미지 빌드 → DB 마이그레이션 → **Google Cloud Run** 배포.
테스트와 빌드가 모두 성공해야 배포됩니다. 데모 데이터 넣기·관리자 계정 관리·에이전트 평가는 Actions에서 수동으로 실행합니다.
