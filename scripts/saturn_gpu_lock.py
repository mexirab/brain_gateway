"""
Exclusive-use lock for Saturn's RTX 3090.

The 3090 normally hosts the expert-model container (Qwen3-32B on
llama-server at 10.0.0.58:8084). Training runs that need the full card
(e.g. contrastive embedding fine-tune per the gleaming plan) must stop
the expert first, then restart it afterward.

Usage:

    from scripts.saturn_gpu_lock import saturn_3090

    with saturn_3090("embedding-finetune-v1"):
        # 3090 is exclusive to this block. Expert container is stopped.
        run_training()
    # Expert container has been restarted and is coming back up.
    # Health is NOT awaited by default — callers that care should poll
    # http://10.0.0.58:8084/health themselves.

Design notes:

- Uses subprocess + ssh. Requires passwordless ssh to
  ``labadmin@10.0.0.58`` (same key the orchestrator already uses for
  the vision / code-agent nodes).
- Stop-on-enter is guarded by try/finally, so a crash inside the block
  still restarts the expert. The start-on-exit is best-effort: if the
  docker start fails, we log loudly but do not re-raise, so the
  original training exception (if any) is not masked.
- Idempotent: if the expert container does not exist, the lock is a
  no-op and prints a warning. If it's already stopped, stop is a no-op.
- Not thread-safe. Only one lock holder at a time — there is no
  locking on top of the container state. The caller is responsible.
- The lock does NOT wait for the expert to become healthy again on
  release. Typical cold-start is ~90-120s on the 3090. Poll the
  /health endpoint yourself if you need certainty before exiting the
  script.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from contextlib import contextmanager
from typing import Iterator

logger = logging.getLogger(__name__)

SATURN_HOST = "labadmin@10.0.0.58"
EXPERT_CONTAINER = "expert-model"
SSH_TIMEOUT_SEC = 15
DOCKER_OP_TIMEOUT_SEC = 30


def _ssh(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={SSH_TIMEOUT_SEC}",
        "-o",
        "StrictHostKeyChecking=accept-new",
        SATURN_HOST,
        *args,
    ]
    return subprocess.run(
        cmd,
        check=check,
        capture_output=True,
        text=True,
        timeout=DOCKER_OP_TIMEOUT_SEC,
    )


def _container_exists() -> bool:
    result = _ssh(
        ["docker", "ps", "-a", "--filter", f"name={EXPERT_CONTAINER}", "--format", "{{.Names}}"],
        check=False,
    )
    return EXPERT_CONTAINER in result.stdout


def _container_running() -> bool:
    result = _ssh(
        ["docker", "ps", "--filter", f"name={EXPERT_CONTAINER}", "--format", "{{.Names}}"],
        check=False,
    )
    return EXPERT_CONTAINER in result.stdout


def stop_expert() -> bool:
    """Stop the expert container. Returns True if a stop was performed."""
    if not _container_exists():
        logger.warning("[saturn_gpu_lock] %s container not found — nothing to stop", EXPERT_CONTAINER)
        return False
    if not _container_running():
        logger.info("[saturn_gpu_lock] %s already stopped", EXPERT_CONTAINER)
        return False
    logger.info("[saturn_gpu_lock] stopping %s to release 3090", EXPERT_CONTAINER)
    _ssh(["docker", "stop", EXPERT_CONTAINER])
    # Brief pause so nvidia-smi reflects the freed VRAM before the caller
    # spawns its own GPU workload.
    time.sleep(2)
    return True


def start_expert() -> bool:
    """Start the expert container. Returns True if a start was performed.

    Does NOT wait for health — the caller decides whether to poll.
    """
    if not _container_exists():
        logger.warning("[saturn_gpu_lock] %s container not found — cannot start", EXPERT_CONTAINER)
        return False
    if _container_running():
        logger.info("[saturn_gpu_lock] %s already running", EXPERT_CONTAINER)
        return False
    logger.info("[saturn_gpu_lock] starting %s (expert will be cold ~90-120s)", EXPERT_CONTAINER)
    _ssh(["docker", "start", EXPERT_CONTAINER])
    return True


@contextmanager
def saturn_3090(holder: str) -> Iterator[None]:
    """Exclusive-use context manager for Saturn's RTX 3090.

    ``holder`` is a free-form string naming who took the lock (e.g.
    ``"embedding-finetune-v1"``). Logged so lock contention is diagnosable.
    """
    logger.info("[saturn_gpu_lock] acquiring 3090 for %s", holder)
    stopped = False
    try:
        stopped = stop_expert()
        yield
    finally:
        if stopped:
            try:
                start_expert()
                logger.info("[saturn_gpu_lock] 3090 released by %s — expert restarting", holder)
            except Exception:  # noqa: BLE001
                # Never mask the caller's original exception.
                logger.exception(
                    "[saturn_gpu_lock] FAILED to restart expert-model after %s — manually: ssh %s 'docker start %s'",
                    holder,
                    SATURN_HOST,
                    EXPERT_CONTAINER,
                )
        else:
            logger.info("[saturn_gpu_lock] no stop performed for %s — nothing to restart", holder)


def _cli() -> int:
    """Ad-hoc CLI for manual operators.

    Usage:
        python scripts/saturn_gpu_lock.py status
        python scripts/saturn_gpu_lock.py stop
        python scripts/saturn_gpu_lock.py start
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if len(sys.argv) != 2 or sys.argv[1] not in {"status", "stop", "start"}:
        print(__doc__, file=sys.stderr)
        print("\nCLI: status | stop | start", file=sys.stderr)
        return 2
    cmd = sys.argv[1]
    if cmd == "status":
        exists = _container_exists()
        running = _container_running() if exists else False
        print(f"container={EXPERT_CONTAINER} exists={exists} running={running}")
        return 0
    if cmd == "stop":
        stop_expert()
        return 0
    if cmd == "start":
        start_expert()
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(_cli())
