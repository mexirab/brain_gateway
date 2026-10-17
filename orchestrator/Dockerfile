FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl tesseract-ocr && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# orchestrator/ is a proper Python package: sources live at /app/orchestrator/
# and /app is on PYTHONPATH so `import orchestrator.xxx` resolves from anywhere.
ENV PYTHONPATH=/app
COPY *.py /app/orchestrator/

# Non-root runtime user. UID/GID default to 1000 to match the host user that
# owns the bind-mounted state (data/app, Chroma, hf_cache, credentials, .env);
# override with ORCHESTRATOR_UID / ORCHESTRATOR_GID in .env (compose passes
# them as build args) when the host user differs. Host dirs created by root
# need a one-time chown — see scripts/orchestrator_state_perms.sh.
ARG APP_UID=1000
ARG APP_GID=1000
RUN (getent group "${APP_GID}" >/dev/null || groupadd --gid "${APP_GID}" jess) \
    && useradd --no-log-init --uid "${APP_UID}" --gid "${APP_GID}" --home-dir /home/jess --create-home \
        --shell /usr/sbin/nologin jess

# Pre-create every mount point the non-root user must own. Docker creates
# missing mount points as root, and a fresh named volume copies the ownership
# of the image directory it is mounted on (brain-audio → /tmp/brain_audio).
# /app/config holds read-only bind-mounted config files
# (e.g. ${GATEWAY_ROOT_PATH}/data/routines.yaml:/app/config/routines.yaml:ro).
RUN mkdir -p /app/config /app/data /app/credentials /tmp/brain_audio \
        /home/jess/.cache/huggingface /home/jess/.ssh /home/jess/.claude/projects \
    && chown -R "${APP_UID}:${APP_GID}" /app/data /app/credentials /tmp/brain_audio /home/jess

ENV HOME=/home/jess \
    HF_HOME=/home/jess/.cache/huggingface
USER jess

EXPOSE 8888

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8888/health || exit 1

CMD ["uvicorn", "orchestrator.orchestrator:app", "--host", "0.0.0.0", "--port", "8888", "--no-server-header"]
