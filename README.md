# backend

캠퍼스팟 FastAPI 백엔드. AI 관련 기능(의도분류/RAG 등)은 별도 배포된
`ai` 서비스(`campuspot-ai` Cloud Run)를 HTTP로 호출해서 사용 —
`AI_SERVICE_URL` + `X-Internal-Secret` 인증.

기준 명세서: [docs/campus-esm-chatbot-spec.md](docs/campus-esm-chatbot-spec.md)
