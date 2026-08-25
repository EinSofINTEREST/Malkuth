.PHONY: help install lint fmt typecheck test test-integration test-e2e check build up down clean

UV ?= uv
RUN := $(UV) run

help:
	@grep -E "^[a-zA-Z_-]+:.*?## .*$$" $(MAKEFILE_LIST) | \
		awk "BEGIN {FS = \":.*?## \"}; {printf \"  \\033[36m%-18s\\033[0m %s\\n\", \$$1, \$$2}"

install: ## 고정된 의존성 설치 (uv.lock 기준)
	$(UV) sync --frozen

lint: ## ruff check + format 검사
	$(RUN) ruff check src tests
	$(RUN) ruff format --check src tests

fmt: ## ruff format 적용 + import 정렬
	$(RUN) ruff format src tests
	$(RUN) ruff check --fix src tests

typecheck: ## mypy (malkuth.core 는 strict)
	$(RUN) mypy

test: ## unit 테스트 + 커버리지 게이트 (>= 70%)
	$(RUN) pytest tests/unit

# 레이어 구현 전에는 수집 대상이 없다 — pytest 의 "no tests collected"(5) 만 통과시키고
# 실제 실패(1)는 그대로 게이트를 막는다
test-integration: ## Docker 기반 통합 테스트
	@$(RUN) pytest tests/integration -m integration --no-cov; \
		status=$$?; [ $$status -eq 0 ] || [ $$status -eq 5 ]

test-e2e: ## 전체 스택 E2E 테스트 (nightly)
	@$(RUN) pytest tests/e2e -m e2e --no-cov; \
		status=$$?; [ $$status -eq 0 ] || [ $$status -eq 5 ]

check: lint typecheck test ## 머지 전 로컬 전체 검증

DOCKER_DIR := deployments/docker

build: ## 에이전트 base image 빌드
	@test -f $(DOCKER_DIR)/agent-base.Dockerfile \
		|| { echo "missing $(DOCKER_DIR)/agent-base.Dockerfile — agent base image is not implemented yet"; exit 1; }
	docker build -t malkuth/agent-base:0.1.0 -f $(DOCKER_DIR)/agent-base.Dockerfile .

up: ## 개발 스택 기동
	@test -f $(DOCKER_DIR)/compose.yaml \
		|| { echo "missing $(DOCKER_DIR)/compose.yaml — dev stack is not implemented yet"; exit 1; }
	docker compose -f $(DOCKER_DIR)/compose.yaml up -d

down: ## 개발 스택 정지
	@test -f $(DOCKER_DIR)/compose.yaml \
		|| { echo "missing $(DOCKER_DIR)/compose.yaml — dev stack is not implemented yet"; exit 1; }
	docker compose -f $(DOCKER_DIR)/compose.yaml down

clean: ## 빌드/캐시 산출물 정리
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov dist
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
