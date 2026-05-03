FROM python:3.12-slim AS builder
WORKDIR /build
COPY requirements.txt .
RUN pip install --quiet --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim
RUN useradd -r -s /sbin/nologin -d /app agentuser
WORKDIR /app
COPY --from=builder /install /usr/local
COPY --chown=agentuser:agentuser main.py .
USER agentuser
ENV PYTHONUNBUFFERED=1
EXPOSE 8002
ENTRYPOINT ["python", "-u", "main.py"]
