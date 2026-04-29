"""File-based recording: each session is one .mjpeg (concat of JPEG frames)."""
from __future__ import annotations

import logging
import queue
import threading
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


class RecordingManager:
    def __init__(self, client, base_dir: Path):
        self.client = client
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._active: Optional[dict] = None

    def is_recording(self) -> bool:
        return self._active is not None

    def status(self) -> dict:
        with self._lock:
            a = self._active
            if a is None:
                return {"recording": False}
            return {
                "recording": True,
                "name": a["path"].stem,
                "frames": a["frames"],
                "duration": time.time() - a["started_at"],
            }

    def start(self) -> dict:
        with self._lock:
            if self._active is not None:
                raise RuntimeError("already recording")
            ts = time.strftime("%Y%m%d_%H%M%S")
            path = self.base_dir / f"{ts}.mjpeg"
            if path.exists():
                # Disambiguate if same-second restart
                path = self.base_dir / f"{ts}_{int(time.time() * 1000) % 1000:03d}.mjpeg"
            q = self.client.subscribe()
            stop_evt = threading.Event()
            entry = {
                "path": path,
                "queue": q,
                "stop": stop_evt,
                "started_at": time.time(),
                "frames": 0,
            }
            t = threading.Thread(target=self._writer, args=(entry,), daemon=True, name="qipower-record")
            entry["thread"] = t
            self._active = entry
            t.start()
            log.info("recording started: %s", path)
            return {"name": path.stem, "path": str(path)}

    def stop(self) -> Optional[dict]:
        with self._lock:
            if self._active is None:
                return None
            entry = self._active
            self._active = None
        entry["stop"].set()
        self.client.unsubscribe(entry["queue"])
        entry["thread"].join(timeout=5)
        log.info("recording stopped: %s (%d frames)", entry["path"], entry["frames"])
        return {
            "name": entry["path"].stem,
            "path": str(entry["path"]),
            "frames": entry["frames"],
            "duration": time.time() - entry["started_at"],
        }

    def _writer(self, entry: dict) -> None:
        path: Path = entry["path"]
        q: queue.Queue = entry["queue"]
        stop: threading.Event = entry["stop"]
        with path.open("wb") as f:
            while not stop.is_set():
                try:
                    jpeg = q.get(timeout=0.5)
                except queue.Empty:
                    continue
                f.write(jpeg)
                entry["frames"] += 1

    def list(self) -> list[dict]:
        items: list[dict] = []
        for p in sorted(self.base_dir.glob("*.mjpeg"), reverse=True):
            try:
                stat = p.stat()
            except OSError:
                continue
            items.append({
                "name": p.stem,
                "size_bytes": stat.st_size,
                "modified": stat.st_mtime,
            })
        return items

    def file_path(self, name: str) -> Optional[Path]:
        # Whitelist: name must not contain path separators or '..'
        if "/" in name or "\\" in name or ".." in name:
            return None
        p = self.base_dir / f"{name}.mjpeg"
        if not p.is_file():
            return None
        return p

    def first_frame(self, name: str) -> Optional[bytes]:
        p = self.file_path(name)
        if p is None:
            return None
        # An .mjpeg file is a concat of JPEGs; first frame = first SOI..EOI
        with p.open("rb") as f:
            data = f.read(2 * 1024 * 1024)  # cap read for safety
        soi = data.find(b"\xff\xd8")
        eoi = data.find(b"\xff\xd9", soi + 2) if soi >= 0 else -1
        if soi < 0 or eoi < 0:
            return None
        return data[soi : eoi + 2]
