#!/usr/bin/env python3
"""Watch RAG folder and re-index on changes."""
import time
import subprocess
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

RAG_SOURCE = Path.home() / "rag" / "nadim_rag"
INGEST_SCRIPT = Path.home() / "personal_rag_app" / "ingest_rag.py"
PERSIST_PATH = Path.home() / ".local/share/chroma/personal_rag"
COLLECTION = "nadim_rag"

class RAGHandler(FileSystemEventHandler):
    def __init__(self):
        self.last_run = 0
        self.cooldown = 10  # Wait 10 seconds after last change before re-indexing
        
    def on_any_event(self, event):
        if event.is_directory:
            return
        if not event.src_path.endswith(('.md', '.txt')):
            return
            
        current_time = time.time()
        if current_time - self.last_run < self.cooldown:
            return
            
        self.last_run = current_time
        print(f"\n📝 Change detected: {event.src_path}")
        print("⏳ Re-indexing in 10 seconds...")
        time.sleep(10)  # Wait for sync to complete
        
        print("🔄 Running ingest...")
        result = subprocess.run([
            "python3", str(INGEST_SCRIPT),
            "--source", str(RAG_SOURCE),
            "--persist", str(PERSIST_PATH),
            "--collection", COLLECTION
        ], capture_output=True, text=True)
        
        if result.returncode == 0:
            print("✅ RAG updated successfully")
        else:
            print(f"❌ Error: {result.stderr}")

if __name__ == "__main__":
    print(f"👀 Watching {RAG_SOURCE} for changes...")
    observer = Observer()
    observer.schedule(RAGHandler(), str(RAG_SOURCE), recursive=True)
    observer.start()
    
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
