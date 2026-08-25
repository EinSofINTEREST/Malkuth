# Malkuth 문서

한국어 | **[English](../en/README.md)**

LangGraph 기반 모듈형 멀티 에이전트 오케스트레이션 프레임워크 **Malkuth** 의 사용자/운영자
문서입니다.

> 개발 룰셋의 원본은 [`.claude/rules/`](../../.claude/rules/README.md) 에 있습니다.
> 본 문서는 요약과 안내를 담당하며, 룰셋과 불일치할 경우 룰셋이 우선하고 문서를 수정해야
> 합니다.

## 목차

| 문서 | 내용 |
|---|---|
| [architecture.md](architecture.md) | 시스템 계층, 상호작용 모델, 실행 모드, 리소스 스코프 |
| [getting-started.md](getting-started.md) | 사전 요구사항, 환경 구성, 첫 솔루션 조립 |
| [modules.md](modules.md) | 모듈 시스템 — 스킬셋/프롬프트셋/메모리셋/그래프/그룹 |
| [testing.md](testing.md) | 테스트 전략, 결정성 규칙, 품질 게이트 |
| [ci/conventions.md](ci/conventions.md) | 저장소 거버넌스 및 CI 설계 규칙 |
| [ci/status-checks.md](ci/status-checks.md) | Required status check 이름의 단일 소스 |

## 언어 정책

- `docs/en/` 이 source of truth — 영어 먼저 작성
- `docs/ko/` 는 동일 구조의 한국어 번역 미러
- 두 버전은 항상 동기화 (`Docs Sync Check` 가 구조 미러를 강제)

## 추가 예정

- `runbooks/` — 운영 복구 절차 (런타임 구현과 함께 추가,
  [05-error-handling.md](../../.claude/rules/05-error-handling.md) 참조)
- `api.md` — Control Plane / Agent Control API 레퍼런스 (인터페이스 구현 이후)
