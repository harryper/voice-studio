FROM python:3.11-slim

# Container runs on UTC by default; force Asia/Shanghai so that
# `datetime.now()` and child-process timestamps (subprocess.run, etc.)
# match the host's wall-clock time used everywhere else.
ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 9999
CMD ["gunicorn", "-w", "2", "--timeout", "1800", "-b", "0.0.0.0:9999", "app:app"]
