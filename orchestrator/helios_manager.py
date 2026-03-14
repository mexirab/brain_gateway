"""
Helios lifecycle management: health checks, SSH start/stop, idle tracking.
"""

import asyncio
import logging
import os
import time

import shared
from metrics import (
    HELIOS_START_COUNT,
    HELIOS_START_LATENCY,
    HELIOS_STOP_COUNT,
)

logger = logging.getLogger(__name__)


async def check_helios_health() -> bool:
    """Check if Helios is running and responsive."""
    try:
        r = await shared._http.get(f"{shared.HELIOS_URL.replace('/v1', '')}/health", timeout=5)
        return r.status_code == 200
    except Exception:
        return False


async def start_helios() -> bool:
    """Start Helios via SSH (paramiko) and wait for it to be ready."""
    import paramiko

    HELIOS_START_COUNT.inc()
    _helios_t0 = time.time()
    logger.info("[EXPERT] Helios is offline, attempting to start...", extra={"component": "helios"})

    helios_ip = os.environ.get("NODE_HELIOS_IP", "10.0.0.195")
    ssh_user = os.environ.get("HELIOS_SSH_USER", "labadmin")
    ssh_key = os.environ.get("HELIOS_SSH_KEY", "/root/.ssh/id_ed25519")

    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.WarningPolicy())
        ssh.connect(
            hostname=helios_ip,
            username=ssh_user,
            key_filename=ssh_key,
            timeout=30,
        )
        stdin, stdout, stderr = ssh.exec_command("sudo systemctl start llama-server", timeout=30)
        exit_code = stdout.channel.recv_exit_status()
        ssh.close()

        if exit_code != 0:
            error_msg = stderr.read().decode()
            logger.error(f"[EXPERT] Failed to start Helios: {error_msg}")
            return False
        logger.info("[EXPERT] SSH command succeeded, waiting for model to load...")
    except Exception as e:
        logger.error(f"[EXPERT] SSH to Helios failed: {e}")
        return False

    logger.info("[EXPERT] Waiting for Helios to load model...")
    for i in range(36):  # 36 * 5 seconds = 3 minutes
        await asyncio.sleep(5)
        if await check_helios_health():
            HELIOS_START_LATENCY.observe(time.time() - _helios_t0)
            logger.info(
                f"[EXPERT] Helios ready after ~{(i + 1) * 5} seconds",
                extra={"component": "helios", "latency_ms": int((time.time() - _helios_t0) * 1000)},
            )
            return True
        logger.debug(f"[EXPERT] Still waiting... ({(i + 1) * 5}s)")

    logger.error("[EXPERT] Helios failed to start within 3 minutes")
    return False


async def stop_helios() -> bool:
    """Stop Helios via SSH to save power."""
    import paramiko

    HELIOS_STOP_COUNT.inc()
    logger.info("[EXPERT] Stopping Helios to save power...", extra={"component": "helios"})

    helios_ip = os.environ.get("NODE_HELIOS_IP", "10.0.0.195")
    ssh_user = os.environ.get("HELIOS_SSH_USER", "labadmin")
    ssh_key = os.environ.get("HELIOS_SSH_KEY", "/root/.ssh/id_ed25519")

    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.WarningPolicy())
        ssh.connect(
            hostname=helios_ip,
            username=ssh_user,
            key_filename=ssh_key,
            timeout=30,
        )
        stdin, stdout, stderr = ssh.exec_command("sudo systemctl stop llama-server", timeout=30)
        exit_code = stdout.channel.recv_exit_status()
        ssh.close()

        if exit_code != 0:
            error_msg = stderr.read().decode()
            logger.error(f"[EXPERT] Failed to stop Helios: {error_msg}")
            return False

        logger.info("[EXPERT] Helios stopped successfully")
        return True
    except Exception as e:
        logger.error(f"[EXPERT] SSH to Helios failed: {e}")
        return False


async def check_helios_idle():
    """Check if Helios should be stopped due to inactivity."""
    if not await check_helios_health():
        return

    if shared._last_helios_request == 0.0:
        return

    idle_timeout = int(os.environ.get("HELIOS_IDLE_TIMEOUT", 1800))
    if idle_timeout <= 0:
        return

    idle_time = time.time() - shared._last_helios_request

    if idle_time > idle_timeout:
        logger.info(
            f"[EXPERT] Helios idle for {idle_time:.0f}s (threshold: {idle_timeout}s), stopping to save power..."
        )
        await stop_helios()
