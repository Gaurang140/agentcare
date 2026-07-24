# Build context is the repo root (see docker-compose.yml: context: ., dockerfile: backend/Dockerfile)
# so requirements.txt (root) and backend/ are both reachable here.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/

RUN useradd --create-home --uid 1000 appuser \
    && chmod +x /app/backend/docker-entrypoint.sh \
    && mkdir -p /app/backend/uploads \
    && chown -R appuser:appuser /app

WORKDIR /app/backend
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:8000/api/health || exit 1

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
