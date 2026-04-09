FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SHADOW_DB_PATH=/data/shadow.db \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=47821

WORKDIR /app

# System deps for building eth-account / coincurve wheels on slim base.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential libssl-dev libffi-dev pkg-config curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY copytrader.py shadow.py webapp.py entrypoint.sh ./
RUN chmod +x entrypoint.sh && mkdir -p /data

EXPOSE 47821
VOLUME ["/data"]

ENTRYPOINT ["./entrypoint.sh"]
