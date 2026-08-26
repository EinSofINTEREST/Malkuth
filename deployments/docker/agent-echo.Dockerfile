# malkuth/agent-echo — 테스트 전용 에이전트 이미지
#
# 모델을 호출하지 않고 입력을 echo 한다. 프로토콜/lifecycle 검증에서
# 모델 비결정성과 API 비용을 배제하기 위한 대역이다 (06 Integration Testing).
FROM malkuth/agent-base:0.1.0

COPY --chown=1000:1000 agents/echo/manifest.yaml /app/manifest.yaml
