# ADR 0005: 프론트→백엔드 연결은 CORS, 세션은 쿠키 대신 브라우저 저장소

## 상태
확정 (2026-09-28)

## 문제
frontend와 backend가 서로 다른 Cloud Run 주소(`campuspot-frontend-…run.app`,
`campuspot-backend-…run.app`)로 배포돼 있어서, 브라우저에서 frontend가 backend를 부르면
다른 출처(cross-origin) 요청이 된다. 또 명세서 초안은 "익명 세션쿠키"로 사용자를 구분하기로
돼 있었는데, 두 주소가 서로 다른 사이트로 취급되면 쿠키가 서드파티 쿠키가 되어 Safari·크롬
등에서 차단될 수 있다 (학생 실사용은 모바일 비중이 높음).

## 검토한 대안
1. **CORS**: backend가 frontend 출처를 허용하고, frontend가 backend 주소로 직접 호출
2. **nginx 리버스 프록시**: frontend의 nginx가 `/api/*`를 backend로 전달해 같은 출처로 만듦
3. 커스텀 도메인 + 로드밸런서로 두 서비스를 한 도메인 아래 배치

## 결정
1번(CORS) + 세션ID는 쿠키가 아니라 **브라우저 localStorage에 보관**.
- backend: `CORSMiddleware`에 `campuspot-frontend-*.run.app` 주소만 허용하는 정규식 사용
  (Cloud Run 주소 두 형식 모두 대응, 주소를 코드에 복사해 넣지 않아도 됨)
- frontend: `src/api/client.ts`에서 backend 주소로 직접 호출, `POST /chat/sessions`가 준
  `session_id`를 localStorage에 저장해 이후 요청에 사용. API 계약(명세서 5-1)은 원래
  session_id를 주고받는 구조라 변경 없음

## 트레이드오프
- localStorage는 쿠키(HttpOnly)보다 스크립트에서 접근 가능해 XSS에 약함 → 세션ID는 익명
  "내 신고 조회" 용도라 탈취돼도 피해가 제한적이라 감수
- 브라우저 저장소를 지우면 "내 신고 조회" 이력이 끊김 → 익명 서비스 특성상 감수

## 왜 이렇게 결정했는지
2번은 쿠키 문제는 없지만 nginx 설정(SNI·Host 헤더·SSE 버퍼링·타임아웃)이 까다롭고,
장애 시 원인이 frontend/nginx/backend 중 어디인지 추적하기 어렵다. 3번은 5.5주 일정 대비
과하다. 1번은 설정이 몇 줄이고, 실패하면 브라우저가 "CORS 에러"로 원인을 바로 보여줘서
이 스택이 처음인 팀원도 디버깅하기 쉽다.
