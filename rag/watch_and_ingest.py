#!/usr/bin/env python3
"""
Watch the RAG source folder and re-index after changes.

Design goals:
- Runs under systemd reliably (prints flushed)
- Uses the SAME Python env as the watcher (sys.executable)
- Debounces bursts of file events (rsync/editor atomic writes)
- Ignores noisy/temporary files and probe files
"""

import sys
import time
import subprocess
import threading
from pathlib import Path

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# --- Paths / config ---
SCRIPT_DIR = Path(__file__).resolve().parent

RAG_SOURCE = Path.home() / "rag" / "nadim_rag"
INGEST_SCRIPT = SCRIPT_DIR / "ingest_rag.py"
PERSIST_PATH = Path.home() / ".local" / "share" / "chroma" / "personal_rag"
COLLECTION = "nadim_rag"

# Run ingest once after changes settle for this many seconds
DEBOUNCE_SECONDS = 60

# Ignore these exact filenames (your probes + common noise)
IGNORE_NAMES = {
    "_service_probe.md",
    "_sync_probe.md",
    ".DS_Store",
}

# Only trigger ingest for these file extensions
TRIGGER_EXTS = {".md", ".txt"}


def log(msg: str) -> None:
    print(msg, flush=True)


def run_ingest() -> int:
    """Run ingest_rag.py using the same interpreter as this watcher."""
    cmd = [
        sys.executable,
        str(INGEST_SCRIPT),
        "--source",
        str(RAG_SOURCE),
        "--persist",
        str(PERSIST_PATH),
        "--collection",
        COLLECTION,
    ]

    log("🔄 Running ingest...")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode == 0:
        log("✅ RAG updated successfully")
    else:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        log("❌ Ingest failed")
        if stdout:
            log(f"--- stdout ---\n{stdout}")
        if stderr:
            log(f"--- stderr ---\n{stderr}")

    return result.returncode


class DebouncedIngestHandler(FileSystemEventHandler):
    """Debounce filesystem events and run ingest once after the burst."""

    def __init__(self, debounce_seconds: int = DEBOUNCE_SECONDS):
        super().__init__()
        self.debounce_seconds = debounce_seconds
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._last_event_path: str | None = None
        self._ingest_running = False

    def _should_ignore(self, event) -> bool:
        if event.is_directory:
            return True

        p = Path(event.src_path)
        name = p.name

        # Ignore hidden files, swap files, temp files, and probes
        if name in IGNORE_NAMES:
            return True
        if name.startswith("."):
            return True
        if name.endswith(("~", ".swp", ".tmp", ".bak")):
            return True

        # Only trigger on content files
        if p.suffix.lower() not in TRIGGER_EXTS:
            return True

        return False

    def _schedule_ingest(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.debounce_seconds, self._debounced_fire)
            self._timer.daemon = True
            self._timer.start()

    def _debounced_fire(self) -> None:
        # Prevent overlapping ingests if something triggers during ingest
        with self._lock:
            if self._ingest_running:
                # If ingest is running, schedule another run after it finishes
                # (this handles long ingests + new edits)
                self._schedule_ingest()
                return
            self._ingest_running = True

        try:
            run_ingest()
        finally:
            with self._lock:
                self._ingest_running = False

    def on_any_event(self, event) -> None:
        if self._should_ignore(event):
            return

        self._last_event_path = event.src_path
        log(f"📝 Change detected: {event.src_path}")
        log(f"⏳ Debouncing for {self.debounce_seconds}s...")
        self._schedule_ingest()


def main() -> int:
    # Basic sanity checks
    if not RAG_SOURCE.exists():
        log(f"❌ RAG source folder not found: {RAG_SOURCE}")
        return 2
    if not INGEST_SCRIPT.exists():
        log(f"❌ ingest_rag.py not found: {INGEST_SCRIPT}")
        return 2

    log(f"👀 Watching {RAG_SOURCE} for changes...")
    observer = Observer()
    observer.schedule(DebouncedIngestHandler(), str(RAG_SOURCE), recursive=True)
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log("🛑 Stopping watcher...")
        observer.stop()

    observer.join()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
