FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=4321 \
    CONFIG_DIR=/config \
    MEDIA_ROOT=/data

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY app /app/app

# Fail the image build early if the application has a Python syntax/import
# problem or if a fresh SQLite schema cannot be initialized.
RUN python -m py_compile /app/app/main.py \
    && CONFIG_DIR=/tmp/arrstream-build python -c "from app.main import init_db; init_db()"

RUN mkdir -p /config /data
VOLUME ["/config", "/data"]
EXPOSE 4321

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://127.0.0.1:4321/api/v1/health || exit 1

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
