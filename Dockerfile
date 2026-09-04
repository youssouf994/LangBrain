FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DB_PATH=/data/langbrain.db

WORKDIR /app

RUN groupadd --system langbrain \
    && useradd --system --gid langbrain --home-dir /app langbrain \
    && mkdir -p /data \
    && chown -R langbrain:langbrain /app /data

COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

COPY --chown=langbrain:langbrain . .

USER langbrain

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/', timeout=3).read()" || exit 1

# Un solo worker: tool registry, HITL manager e MemorySaver sono in memoria di processo.
CMD ["python", "-m", "uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
