FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CQA_DATABASE_PATH=/var/lib/replimetr/cqa.sqlite3

WORKDIR /app
COPY pyproject.toml requirements-lock.txt ./
COPY backend/ ./backend/
RUN python -m pip install --no-cache-dir -c requirements-lock.txt . \
    && groupadd --gid 10001 replimetr \
    && useradd --uid 10001 --gid 10001 --no-create-home replimetr \
    && mkdir -p /var/lib/replimetr \
    && chown 10001:10001 /var/lib/replimetr

USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).close()"]

# One process owns the in-process batch consumer. SIGTERM reaches Uvicorn directly.
CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log", "--timeout-graceful-shutdown", "200"]
