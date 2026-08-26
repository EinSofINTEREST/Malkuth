# web-search 0.2.0

웹 검색과 페이지 본문 추출을 제공하는 레퍼런스 스킬셋.

## Tools

| Tool | 설명 | timeout |
|---|---|---|
| `search` | 웹 검색 상위 결과 | 30s |
| `fetch_page` | URL 본문 텍스트 추출 | 60s |

## 요구 사항

- env: `SEARCH_API_KEY` (에이전트 manifest 의 `env_allowlist` 에도 있어야 함)
- python: >= 3.12

## 주의

`fetch_page` 가 반환하는 페이지 본문은 **신뢰하지 않는 입력**이다.
프롬프트에 주입할 때 경계를 표시하고, 본문 안의 지시문을 시스템 지시로
승격하지 않는다.
