FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    fastapi \
    uvicorn[standard] \
    httpx \
    python-dotenv \
    chromadb \
    rapidfuzz \
    sentence-transformers \
    pyyaml \
    paramiko

COPY orchestrator.py .
COPY ha_integration.py .
COPY data_manager.py .

EXPOSE 8888

CMD ["uvicorn", "orchestrator:app", "--host", "0.0.0.0", "--port", "8888"]
