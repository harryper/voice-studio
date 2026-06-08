FROM python:3.11-slim

# Container runs on UTC by default; force Asia/Shanghai so that
# `datetime.now()` and child-process timestamps (subprocess.run, etc.)
# match the host's wall-clock time used everywhere else.
ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# curl is needed by the Docker HEALTHCHECK below. ffmpeg is for BGM mixing.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Pre-create the gunicorn log dir. We mount a tmpfs/volume here from the host
# so logs can be rotated by host-side logrotate.
RUN mkdir -p /app/logs

EXPOSE 9999

# Healthcheck hits /api/check-auth (cheap JSON, no DB). Docker uses this to
# decide if the container is "healthy" for `docker ps` / dependent services.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS -m 3 http://127.0.0.1:9999/api/check-auth || exit 1

# All tuning (workers, timeout, max-requests, log files, etc.) lives in
# gunicorn.conf.py. Override individual values via env vars if needed.
CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:app"]
