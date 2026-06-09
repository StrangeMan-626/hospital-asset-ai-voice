FROM python:3.12.11

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=9000

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./

RUN pip install --upgrade pip setuptools wheel \
    && pip install -r requirements.txt

RUN groupadd --system app \
    && useradd --system --gid app --create-home --home-dir /home/app app

COPY --chown=app:app app ./app

USER app

EXPOSE 9000

HEALTHCHECK --interval=30m --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; port = os.getenv('PORT', '9000'); urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=3)"]

CMD ["sh", "-c", "exec python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-9000}"]
