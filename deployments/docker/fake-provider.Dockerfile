# fake-provider — E2E 전용 결정적 모델 provider
#
# 실제 LLM 을 호출하지 않는다 (06 Testing 3). 같은 입력이면 항상 같은 출력이라
# E2E 가 모델 비결정성 때문에 흔들리지 않는다.
FROM python:3.12-slim

RUN groupadd --gid 1000 provider \
    && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin provider

COPY --chown=1000:1000 deployments/docker/fake-provider/server.py /app/server.py

ENV PYTHONUNBUFFERED=1
WORKDIR /app
USER 1000:1000
EXPOSE 8000

HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/', timeout=3).status == 200 else 1)"]

ENTRYPOINT ["python", "/app/server.py"]
