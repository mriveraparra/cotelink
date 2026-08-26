FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CHROME_BIN=/usr/bin/chromium \
    AUTOWEB_EDGE_FALLBACK=chrome \
    HOME=/tmp \
    SE_CACHE_PATH=/tmp/selenium

RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium chromium-driver ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin autoweb

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py wsgi.py ./
COPY static ./static

USER autoweb

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl --fail --silent http://127.0.0.1:5000/api/health || exit 1

# Un worker conserva un único scheduler; los threads atienden solicitudes concurrentes.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "4", "--timeout", "180", "--access-logfile", "-", "--error-logfile", "-", "wsgi:app"]
