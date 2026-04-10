FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl tesseract-ocr && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py .

# Pre-create /app/config so docker-compose can bind-mount config files into it
# (e.g. ${GATEWAY_ROOT_PATH}/data/routines.yaml:/app/config/routines.yaml:ro).
# /app/data is occupied by a named volume (finance-data), so config files that
# ship from the repo live at /app/config/ instead.
RUN mkdir -p /app/config

EXPOSE 8888

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8888/health || exit 1

CMD ["uvicorn", "orchestrator:app", "--host", "0.0.0.0", "--port", "8888", "--no-server-header"]
