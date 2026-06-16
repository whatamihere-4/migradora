FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends coreutils \
    && rm -rf /var/lib/apt/lists/*

COPY shared/requirements.txt /app/shared/requirements.txt
COPY downloader/requirements.txt /app/downloader/requirements.txt
RUN pip install --no-cache-dir \
    -r /app/shared/requirements.txt \
    -r /app/downloader/requirements.txt

COPY shared/src /app/shared/src
COPY downloader/src /app/downloader/src

ENV PYTHONPATH=/app/shared/src:/app/downloader/src
ENV PYTHONUNBUFFERED=1

HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=15s \
    CMD python -c "import os,time; p='/data/state/downloader.heartbeat'; exit(0 if os.path.exists(p) and time.time()-float(open(p).read())<120 else 1)"

CMD ["python", "-m", "migradora"]
