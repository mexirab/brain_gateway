FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    fastapi \
    uvicorn[standard] \
    httpx \
    python-dotenv \
    chromadb \
    rapidfuzz

COPY orchestrator.py .
COPY ha_integration.py .

EXPOSE 8888

CMD ["uvicorn", "orchestrator:app", "--host", "0.0.0.0", "--port", "8888"]
