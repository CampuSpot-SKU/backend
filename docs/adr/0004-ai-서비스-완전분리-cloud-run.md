# ADR 0004: AI 로직을 별도 Cloud Run 서비스로 완전 분리

## 상태
확정 (0003을 대체함)

## 문제
0003에서는 `ai`를 레포만 분리하고 backend가 pip 패키지로 설치해서 쓰는 방식으로
정했었음. 그런데 AI 관련 로직(의도분류/RAG/우선순위/탐지/예측)의 비중과 복잡도가
커지면서, 코드 저장소만 분리돼있는 걸로는 부족하다고 판단 — 실제로 독립적인
배포/버전 단위로 관리되길 원함.

## 결정
`ai`를 별도 FastAPI 서비스로 감싸서 자체 Cloud Run 서비스(`campuspot-ai`)로 배포.
backend는 의도분류/RAG가 필요할 때 HTTP로 이 서비스를 호출.

- 통신: backend → ai, REST(JSON). `POST /api/v1/intent/classify`, `POST /api/v1/rag/answer`
- 인증: Cloud Run은 `--allow-unauthenticated`로 열되, 모든 요청에 `X-Internal-Secret`
  헤더를 요구하고 `AI_SERVICE_SECRET` 값과 대조 (backend/ai 양쪽 GitHub Secret에 동일값 등록)
  — Cloud Run IAM 기반 서비스 계정 인증(ID 토큰 방식)이 더 안전하지만, 그만큼 구현
  비용이 커서 5.5주 일정엔 이 방식이 더 맞다고 판단
- 배치 작업 이관: 반복탐지(`detection-scan`)·재발예측(`prediction-update`)·공지크롤링
  (`crawl-notices`)은 DB를 직접 읽고 쓰는 AI 도메인 로직이라 Cloud Scheduler가
  backend를 거치지 않고 ai 서비스를 직접 호출하도록 변경. SLA/에스컬레이션 체크만
  reports 도메인이라 backend에 남김
- DB 접속: ai 서비스도 backend와 동일한 Supabase DB에 직접 접속 (탐지/예측/크롤링이
  reports/regulations 관련 테이블을 읽고 써야 하므로) — DB는 공유하되 서비스는 분리하는
  일반적인 패턴

## 트레이드오프 (인지하고 감수하는 것)
- 배포 순서 의존성 생김: ai를 먼저 배포해서 URL을 받아야 backend 시크릿에 반영 가능
- 네트워크 호출 1홉 추가로 인한 레이턴시 증가, ai 서비스 장애 시 backend의 챗봇 기능도
  같이 실패 (→ 10-6 runbook에 "ai 서비스 상태 확인"을 장애 판단 체크리스트에 추가 필요)
- 서비스 계정 키 하나(`GCP_SA_KEY`)를 세 레포 시크릿에 중복 등록해야 함 (관리 포인트 늘어남)

## 왜 그럼에도 이 방향으로 결정했는지
AI 로직이 프로젝트의 핵심이자 계속 커지는 영역이라, 코드 관리 차원을 넘어 독립적인
배포·롤백·스케일링 단위로 다루는 게 낫다고 판단. 5.5주 일정 안에서도 Cloud Run 배포
자체는 이미 backend/frontend에서 검증된 패턴이라 세 번째 서비스를 추가하는 추가
비용이 크지 않음.
