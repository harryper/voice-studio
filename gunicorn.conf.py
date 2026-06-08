# Gunicorn config for voice-studio Web.
# Loaded by gunicorn when CMD points at this file (see Dockerfile / docker-compose).

import multiprocessing
import os


bind = "0.0.0.0:9999"

# Workers: 2 sync workers is enough for single-user traffic; bump if concurrent
# cron + UI use ever overlaps.
workers = int(os.environ.get("GUNICORN_WORKERS", "2"))
worker_class = "sync"

# 30 minutes is intentional: TTS for ~20 min audio can take 10-15 min on Azure.
# Anything slower than that is a hung request and should fail loudly.
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "1800"))
graceful_timeout = int(os.environ.get("GUNICORN_GRACEFUL_TIMEOUT", "30"))
keepalive = int(os.environ.get("GUNICORN_KEEPALIVE", "5"))

# Memory-leak guard: recycle workers after N requests so a slow leak cannot
# crash the whole pod. Jitter spreads the recycling across workers.
max_requests = int(os.environ.get("GUNICORN_MAX_REQUESTS", "500"))
max_requests_jitter = int(os.environ.get("GUNICORN_MAX_REQUESTS_JITTER", "100"))

# Logging: write access + error to files inside the container. The host then
# rotates them via the logrotate snippet installed by deploy.
accesslog = os.environ.get("GUNICORN_ACCESS_LOG", "/app/logs/access.log")
errorlog = os.environ.get("GUNICORN_ERROR_LOG", "/app/logs/error.log")
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")
access_log_format = (
    '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s '
    '"%(f)s" "%(a)s" %(L)ss'
)

# Process naming helps `ps` and `docker top` identify the master vs workers.
proc_name = "voice-studio-web"

# Preload app: load the Flask app once in the master, then fork workers.
# Trade-off: workers share read-only memory but cannot mutate globals freely.
# We do not mutate globals in this app, so preload is safe and shaves RAM.
preload_app = True

# Send SIGTERM to workers on master shutdown, then SIGKILL after timeout.
worker_exit_on_app_exit = True
