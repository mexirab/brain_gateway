# syntax=docker/dockerfile:1.7
#
# Coder box: Qwen Code in a disposable, offline container (docs/CODER.md).
#
# Built only through compose (`docker compose --profile coder build coder`),
# which passes the freshly built orchestrator image as the build context
# `jess-orchestrator-base` (additional_contexts: service:orchestrator). So the
# box gets exactly Jess's Python dependencies and the repo's pytest suite runs
# inside, without a second multi-GB pip install. A plain `docker build` needs
# `--build-context jess-orchestrator-base=docker-image://<orchestrator image>`
# (the CI docker-build job does that).
#
# Nothing here is started by `docker compose up`: scripts/jess-coder runs one
# fresh `docker run --rm` per session with the hardening flags (read-only root,
# no capabilities, coder-net only). The image itself carries no secret.
FROM jess-orchestrator-base

USER root

# git: the box clones the repo bundle and diffs against its base.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Node 22, official tarball, checksum-pinned (SHASUMS256.txt of the release).
ARG NODE_VERSION=22.23.4
ARG NODE_SHA256=b8492284c76a54baf58d61668dd0e41ee0fc98adabf9503d30f05d08f55b4406
ADD --checksum=sha256:${NODE_SHA256} \
    https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.gz /tmp/node.tar.gz
RUN tar -xzf /tmp/node.tar.gz -C /usr/local --strip-components=1 --no-same-owner \
        --exclude='*/include' --exclude='*/share' --exclude='*/CHANGELOG.md' --exclude='*/README.md' \
    && rm /tmp/node.tar.gz \
    && node --version

# Qwen Code, pinned by the committed lockfile (integrity hashes), no install
# scripts. Bump: edit package.json, regenerate the lock (docs/CODER.md).
WORKDIR /opt/jess-coder/qwen
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund --omit=dev \
    && npm cache clean --force \
    && ln -s /opt/jess-coder/qwen/node_modules/@qwen-code/qwen-code/cli-entry.js /usr/local/bin/qwen \
    && chmod 0755 node_modules/@qwen-code/qwen-code/vendor/ripgrep/x64-linux/rg \
    && qwen --version \
    && node_modules/@qwen-code/qwen-code/vendor/ripgrep/x64-linux/rg --version

# Test tools for the repo's suite (offline inside the box: nothing can be
# installed later).
RUN pip install --no-cache-dir "pytest==9.2.0" "pytest-asyncio==1.5.0" "respx==0.23.1"

# The box runs as its own uid, which no account on Jupiter has (1000 is the
# owner and the orchestrator, 1001 another login): host git then refuses the
# box's repo as "dubious ownership" instead of running hooks or fsmonitor from
# a .git/config the model wrote. Same number as BOX_UID in scripts/jess-coder.
ARG CODER_UID=61001
RUN groupadd --gid "${CODER_UID}" coder \
    && useradd --no-log-init --uid "${CODER_UID}" --gid "${CODER_UID}" --home-dir /home/coder --no-create-home \
        --shell /bin/bash coder \
    && mkdir -p /home/coder /work /bundle /etc/qwen-code \
    && chown "${CODER_UID}:${CODER_UID}" /home/coder /work

# Jess's tests import orchestrator.shared, which loads the pinned embedding
# model (orchestrator/embedding_pin.py) at import time. The box is offline, so
# the image carries that exact model. Read-only at run time; transformers puts
# the model's remote code in HF_MODULES_CACHE (a tmpfs) when it loads.
ENV HF_HOME=/opt/jess-coder/hf
RUN python3 -c "from sentence_transformers import SentenceTransformer; \
from orchestrator.embedding_pin import DEFAULT_EMBEDDING_MODEL as m, load_kwargs; \
SentenceTransformer(m, **load_kwargs(m))" \
    && rm -rf /opt/jess-coder/hf/modules /opt/jess-coder/hf/token /opt/jess-coder/hf/stored_tokens \
    && chmod -R a+rX,go-w /opt/jess-coder/hf

# Jess's code comes from the session's clone, never from the base image.
RUN rm -rf /app/orchestrator

# Locked Qwen Code config (system scope beats user and workspace settings).
# GATE_URL is rendered in at build time from CODER_GATE_ADDRESS (compose arg).
ARG GATE_URL=http://10.213.250.2:8888/api/coder/v1
ARG CONTEXT_WINDOW=131072
COPY settings.json /etc/qwen-code/settings.json
RUN sed -i -e "s#@GATE_URL@#${GATE_URL}#" -e "s#@CONTEXT_WINDOW@#${CONTEXT_WINDOW}#" /etc/qwen-code/settings.json \
    && python3 -c "import json; json.load(open('/etc/qwen-code/settings.json'))" \
    && ! grep -q '@[A-Z_]*@' /etc/qwen-code/settings.json \
    && chmod 0444 /etc/qwen-code/settings.json

COPY system-prompt.md /opt/jess-coder/
COPY selftest.py entrypoint.py /opt/jess-coder/
RUN chmod 0444 /opt/jess-coder/*.md /opt/jess-coder/*.py

ENV HOME=/home/coder \
    PYTHONPATH= \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_MODULES_CACHE=/tmp/hf-modules \
    QWEN_CODE_SUPPRESS_YOLO_WARNING=1 \
    QWEN_USAGE_STATISTICS_ENABLED=false \
    NO_UPDATE_NOTIFIER=1 \
    npm_config_offline=true \
    GIT_CONFIG_NOSYSTEM=1

USER coder
WORKDIR /work
HEALTHCHECK NONE
ENTRYPOINT ["python3", "-I", "/opt/jess-coder/entrypoint.py"]
CMD ["selftest"]
