"""
Model server lifecycle management: health checks, SSH start/stop, idle tracking.

Generalized from helios_manager.py for the v7 unified architecture.
Manages the primary model server (any GPU node running llama.cpp, vLLM, etc.).
"""

import asyncio
import logging
import os
import time

import shared
from metrics import (
    HELIOS_START_COUNT as MODEL_START_COUNT,
)
from metrics import (
    HELIOS_START_LATENCY as MODEL_START_LATENCY,
)
from metrics import (
    HELIOS_STOP_COUNT as MODEL_STOP_COUNT,
)

logger = logging.getLogger(__name__)

# Allowed SSH command prefixes for security — prevents arbitrary command execution
_ALLOWED_CMD_PREFIXES = (
    "sudo systemctl start",
    "sudo systemctl stop",
    "sudo systemctl restart",
    "systemctl start",
    "systemctl stop",
    "systemctl restart",
)


# Shell metacharacters that indicate command chaining/injection
_SHELL_METACHARACTERS = set(";|&$`")


def _validate_ssh_cmd(cmd: str, label: str) -> bool:
    """Validate SSH command against allowed prefixes and reject shell injection."""
    if not any(cmd.startswith(prefix) for prefix in _ALLOWED_CMD_PREFIXES):
        logger.error(
            "[MODEL] Rejected %s command '%s' — must start with one of: %s",
            label,
            cmd[:80],
            ", ".join(_ALLOWED_CMD_PREFIXES),
        )
        return False

    # Reject commands containing shell metacharacters (prevents chaining attacks)
    if any(c in _SHELL_METACHARACTERS for c in cmd):
        logger.error(
            "[MODEL] Rejected %s command '%s' — contains shell metacharacters",
            label,
            cmd[:80],
        )
        return False

    return True


async def check_model_health() -> bool:
    """Check if the primary model server is running and responsive."""
    url = shared.MODEL_URL if shared.UNIFIED_MODE else shared.HELIOS_URL
    try:
        r = await shared._http.get(f"{url.replace('/v1', '')}/health", timeout=5)
        return r.status_code == 200
    except Exception as e:
        logger.debug("[MODEL] Health check failed for %s: %s", url, e)
        return False


async def start_model_server() -> bool:
    """Start the model server via SSH (paramiko) and wait for it to be ready."""
    import paramiko

    MODEL_START_COUNT.inc()
    _t0 = time.time()
    logger.info("[MODEL] Model server is offline, attempting to start...", extra={"component": "model"})

    server_ip = os.environ.get("MODEL_SERVER_IP", os.environ.get("NODE_HELIOS_IP", "10.0.0.195"))
    ssh_user = os.environ.get("MODEL_SSH_USER", os.environ.get("HELIOS_SSH_USER", "labadmin"))
    ssh_key = os.environ.get("MODEL_SSH_KEY", os.environ.get("HELIOS_SSH_KEY", "/root/.ssh/id_ed25519"))
    start_cmd = os.environ.get("MODEL_START_CMD", "sudo systemctl start llama-server")

    if not _validate_ssh_cmd(start_cmd, "start"):
        return False

    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.WarningPolicy())
        ssh.connect(
            hostname=server_ip,
            username=ssh_user,
            key_filename=ssh_key,
            timeout=30,
        )
        stdin, stdout, stderr = ssh.exec_command(start_cmd, timeout=30)
        exit_code = stdout.channel.recv_exit_status()
        ssh.close()

        if exit_code != 0:
            error_msg = stderr.read().decode()
            logger.error("[MODEL] Failed to start model server: %s", error_msg)
            return False
        logger.info("[MODEL] SSH command succeeded, waiting for model to load...")
    except Exception as e:
        logger.error("[MODEL] SSH to model server failed: %s", e)
        return False

    logger.info("[MODEL] Waiting for model server to load model...")
    for i in range(36):  # 36 * 5 seconds = 3 minutes
        await asyncio.sleep(5)
        if await check_model_health():
            MODEL_START_LATENCY.observe(time.time() - _t0)
            logger.info(
                "[MODEL] Model server ready after ~%d seconds",
                (i + 1) * 5,
                extra={"component": "model", "latency_ms": int((time.time() - _t0) * 1000)},
            )
            return True
        logger.debug("[MODEL] Still waiting... (%ds)", (i + 1) * 5)

    logger.error("[MODEL] Model server failed to start within 3 minutes")
    return False


async def stop_model_server() -> bool:
    """Stop the model server via SSH to save power."""
    import paramiko

    MODEL_STOP_COUNT.inc()
    logger.info("[MODEL] Stopping model server to save power...", extra={"component": "model"})

    server_ip = os.environ.get("MODEL_SERVER_IP", os.environ.get("NODE_HELIOS_IP", "10.0.0.195"))
    ssh_user = os.environ.get("MODEL_SSH_USER", os.environ.get("HELIOS_SSH_USER", "labadmin"))
    ssh_key = os.environ.get("MODEL_SSH_KEY", os.environ.get("HELIOS_SSH_KEY", "/root/.ssh/id_ed25519"))
    stop_cmd = os.environ.get("MODEL_STOP_CMD", "sudo systemctl stop llama-server")

    if not _validate_ssh_cmd(stop_cmd, "stop"):
        return False

    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.WarningPolicy())
        ssh.connect(
            hostname=server_ip,
            username=ssh_user,
            key_filename=ssh_key,
            timeout=30,
        )
        stdin, stdout, stderr = ssh.exec_command(stop_cmd, timeout=30)
        exit_code = stdout.channel.recv_exit_status()
        ssh.close()

        if exit_code != 0:
            error_msg = stderr.read().decode()
            logger.error("[MODEL] Failed to stop model server: %s", error_msg)
            return False

        logger.info("[MODEL] Model server stopped successfully")
        return True
    except Exception as e:
        logger.error("[MODEL] SSH to model server failed: %s", e)
        return False


async def check_model_idle():
    """Check if the model server should be stopped due to inactivity."""
    if not await check_model_health():
        return

    if shared._last_helios_request == 0.0:
        return

    idle_timeout = int(os.environ.get("MODEL_IDLE_TIMEOUT", os.environ.get("HELIOS_IDLE_TIMEOUT", 1800)))
    if idle_timeout <= 0:
        return

    idle_time = time.time() - shared._last_helios_request

    if idle_time > idle_timeout:
        logger.info(
            "[MODEL] Model server idle for %.0fs (threshold: %ds), stopping to save power...",
            idle_time,
            idle_timeout,
        )
        await stop_model_server()


# Backward-compat aliases for helios_manager imports
check_helios_health = check_model_health
start_helios = start_model_server
stop_helios = stop_model_server
check_helios_idle = check_model_idle
