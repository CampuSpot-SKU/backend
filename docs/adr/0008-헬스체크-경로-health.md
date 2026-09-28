# ADR 0008: 헬스체크 경로를 `/healthz`에서 `/health`로 변경

## 상태
확정 (2026-09-28)

## 문제
명세서대로 backend·ai에 `/healthz`를 구현했지만, 배포된 서비스에서 `/healthz`를 호출하면
항상 404가 났다. 앱 코드에는 경로가 등록돼 있었다(`/openapi.json`에서 확인). Cloud Run이
z로 끝나는 일부 경로(`/healthz` 등)를 예약해서 요청이 컨테이너까지 전달되지 않기 때문.

## 결정
두 서비스 모두 `/health`로 변경. backend의 `/health`는 DB에 `SELECT 1`을 실행해
`{"status":"ok","db":"ok"}`처럼 DB 연결 상태까지 알려준다.

## 영향
- 배포 확인·장애 대응(runbook)·Supabase 비활성 일시정지 방지용 주기 호출이 모두 `/health` 사용
- 다른 경로를 새로 만들 때도 z로 끝나는 이름은 피할 것
