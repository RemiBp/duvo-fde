FROM python:3.11-slim

# Stdio MCP — no inbound TCP; logs flush immediately for Cloud Logging.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Non-root runtime identity (Korral GCP worker spawns container with stdio pipes).
RUN groupadd --system duvogroup \
    && useradd --system --gid duvogroup --home-dir /app --shell /usr/sbin/nologin duvouser

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=duvouser:duvogroup server.py .
COPY --chown=duvouser:duvogroup secrets/store-keys.example.json secrets/store-keys.json

USER duvouser

# MCP JSON-RPC over stdin/stdout only — parent process owns the pipes.
ENTRYPOINT ["python", "server.py"]
