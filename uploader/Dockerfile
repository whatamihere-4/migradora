FROM python:3.12-slim

WORKDIR /app

COPY shared/requirements.txt /app/shared/requirements.txt
COPY uploader/requirements.txt /app/uploader/requirements.txt
RUN pip install --no-cache-dir \
    -r /app/shared/requirements.txt \
    -r /app/uploader/requirements.txt

COPY shared/src /app/shared/src
COPY uploader/src /app/uploader/src

ENV PYTHONPATH=/app/shared/src:/app/uploader/src
ENV PYTHONUNBUFFERED=1

HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=15s \
    CMD python -c "import os,time; p='/data/state/uploader.heartbeat'; exit(0 if os.path.exists(p) and time.time()-float(open(p).read())<120 else 1)"

CMD ["python", "-m", "migradora"]
