"""Lightweight live invalidation stream for the single-process Atlas server.

SSE intentionally carries no place records. Clients re-fetch only their current
viewport from the existing API. No new runtime dependencies or database tables.
"""

from __future__ import annotations

import json
import sqlite3
from threading import BoundedSemaphore, Condition
from time import monotonic

from .store import session


class LiveChanges:
    """Bounded server-sent events stream backed by SQLite's committed change log.

    Wakes immediately after same-process HTTP commits. Rechecks DB every 1 s
    to discover edits made by CLI jobs or another process sharing the same DB.
    Each connection costs one ThreadingHTTPServer thread: do not expose the
    development server publicly as a high-concurrency broadcast service.
    """

    def __init__(self, db_path, capacity=32):
        self.db_path = db_path
        self.condition = Condition()
        self.generation = 0
        self.slots = BoundedSemaphore(capacity)

    def notify(self):
        with self.condition:
            self.generation += 1
            self.condition.notify_all()

    def revision(self):
        with session(self.db_path) as db:
            return int(db.execute("SELECT COALESCE(MAX(seq),0) FROM changes").fetchone()[0])

    def serve(self, handler):
        if not self.slots.acquire(blocking=False):
            return handler.json(503, {"error": "Quá nhiều kết nối trực tiếp; vui lòng kết nối lại."})
        try:
            self._stream(handler)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            # The browser closed the connection during map navigation/unload.
            # ConnectionAbortedError is the Windows flavor of an aborted peer
            # connection (WinError 10053) and must not escape to the log.
            pass
        except sqlite3.Error:
            # A broken DB connection ends this SSE response; EventSource retries.
            pass
        finally:
            self.slots.release()

    def _stream(self, handler):
        with self.condition:
            observed = self.generation
        latest = self.revision()
        raw_cursor = handler.headers.get("Last-Event-ID", "")
        try:
            cursor = min(max(int(raw_cursor), 0), latest) if raw_cursor else latest
        except ValueError:
            cursor = latest

        # One SSE stream per tab, with periodic reconnect to release idle slots.
        handler.close_connection = True
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-cache, no-transform")
        handler.send_header("X-Accel-Buffering", "no")
        handler.send_header("Connection", "close")
        handler.end_headers()

        def emit(event, revision):
            body = json.dumps({"revision": revision}, separators=(",", ":"))
            handler.wfile.write(f"id: {revision}\nevent: {event}\ndata: {body}\n\n".encode())
            handler.wfile.flush()

        handler.wfile.write(b"retry: 1500\n\n")
        emit("ready", cursor)
        last_heartbeat = monotonic()
        expiry = last_heartbeat + 120

        while monotonic() < expiry:
            # In-process HTTP writes wake this immediately; external SQLite
            # writers are discovered by the 1-second periodic checkpoint.
            with self.condition:
                woke = self.generation != observed
                if not woke:
                    self.condition.wait(timeout=1.0)
                    woke = self.generation != observed
                observed = self.generation

            latest = self.revision()
            if latest != cursor:
                cursor = latest
                emit("changed", cursor)
                last_heartbeat = monotonic()
            elif woke:
                # A same-process write may commit without a new history row
                # (a no-op upsert still touches updated_at and can reorder
                # listings), so notify-driven wakes always invalidate clients.
                emit("changed", cursor)
                last_heartbeat = monotonic()
            elif monotonic() - last_heartbeat >= 15:
                handler.wfile.write(b": heartbeat\n\n")
                handler.wfile.flush()
                last_heartbeat = monotonic()
