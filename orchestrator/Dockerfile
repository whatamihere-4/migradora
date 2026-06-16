FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl coreutils \
    && rm -rf /var/lib/apt/lists/*

COPY shared/requirements.txt /app/shared/requirements.txt
COPY orchestrator/requirements.txt /app/orchestrator/requirements.txt
RUN pip install --no-cache-dir \
    -r /app/shared/requirements.txt \
    -r /app/orchestrator/requirements.txt

COPY shared/src /app/shared/src
COPY orchestrator/src /app/orchestrator/src
COPY scripts/jd2-init-config.sh /app/scripts/jd2-init-config.sh
COPY jd2/config-templates /templates

ENV PYTHONPATH=/app/shared/src:/app/orchestrator/src
ENV PYTHONUNBUFFERED=1
ENV JD2_CONFIG_DIR=/data/jd2/config

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=30s \
    CMD curl -f http://localhost:8080/health || exit 1

CMD ["python", "-m", "migradora", "run"]
