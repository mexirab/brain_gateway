FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    fastapi \
    uvicorn[standard] \
    httpx \
    python-dotenv \
    chromadb \
    rapidfuzz \
    sentence-transformers \
    pyyaml \
    paramiko \
    apscheduler \
    google-auth \
    google-auth-oauthlib \
    prometheus-client \
    ynab

COPY *.py .

EXPOSE 8888

CMD ["uvicorn", "orchestrator:app", "--host", "0.0.0.0", "--port", "8888", "--no-server-header"]
