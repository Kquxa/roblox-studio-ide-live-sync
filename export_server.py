#!/usr/bin/env python3
"""
Bidirectional Roblox live-sync server.

- Listens on localhost:34873
- Accepts Studio -> disk updates over HTTP
- Watches disk for IDE changes
- Exposes change events so the Studio plugin can pull updates
"""

import hashlib
import http.server
import json
import os
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    WATCHDOG_AVAILABLE = True
except ImportError:
    FileSystemEventHandler = object
    Observer = None
    WATCHDOG_AVAILABLE = False

HOST = "127.0.0.1"
PORT = 34873
SYNC_ROUTE = "/sync"
MANIFEST_ROUTE = "/manifest"
FILE_ROUTE = "/file"
EVENTS_ROUTE = "/events"
PROJECT_ROOT = "MyGame"
BASE_DIR = Path(os.getcwd()).resolve()
POLL_INTERVAL_SECONDS = 1.0
WRITE_SUPPRESSION_SECONDS = 3.0


def sanitize_segment(value, field_name):
    """Validate a single path segment to avoid invalid or unsafe paths."""
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    segment = value.strip()
    if not segment:
        raise ValueError(f"{field_name} cannot be empty")
    if segment in {".", ".."} or "/" in segment or "\\" in segment:
        raise ValueError(f"{field_name} contains invalid path characters")
    return segment


def rojo_filename(script_name, script_type):
    """Map Roblox script types to Rojo-compatible file names."""
    name = sanitize_segment(script_name, "name")
    if script_type == "Script":
        return f"{name}.server.luau"
    if script_type == "LocalScript":
        return f"{name}.client.luau"
    if script_type == "ModuleScript":
        return f"{name}.luau"
    raise ValueError("type must be Script, LocalScript, or ModuleScript")


def project_src_root():
    return BASE_DIR / PROJECT_ROOT / "src"


def is_within_root(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def infer_script_type(filename):
    if filename.endswith(".server.luau"):
        return "Script", filename[: -len(".server.luau")]
    if filename.endswith(".client.luau"):
        return "LocalScript", filename[: -len(".client.luau")]
    if filename.endswith(".luau"):
        return "ModuleScript", filename[: -len(".luau")]
    return None, None


def safe_relpath(path, root):
    rel = path.relative_to(root)
    return rel.as_posix()


def hash_text(value):
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def parse_rel_file(rel_file):
    parts = Path(rel_file).parts
    if len(parts) < 2:
        raise ValueError("relFile must include service and filename")

    service = sanitize_segment(parts[0], "service")
    filename = parts[-1]
    script_type, name = infer_script_type(filename)
    if not script_type or not name:
        raise ValueError("relFile does not point to a supported script file")

    path_parts = [sanitize_segment(part, "path[]") for part in parts[1:-1]]
    sanitize_segment(name, "name")
    return {
        "service": service,
        "path": path_parts,
        "name": name,
        "type": script_type,
        "relFile": Path(*parts).as_posix(),
    }


def build_target(record):
    """Build output file path and return target path metadata."""
    service = sanitize_segment(record.get("service"), "service")
    name = record.get("name")
    script_type = record.get("type")

    raw_path = record.get("path", [])
    if not isinstance(raw_path, list):
        raise ValueError("path must be an array")

    safe_path_parts = [sanitize_segment(part, "path[]") for part in raw_path]

    root = project_src_root()
    folder = root / service
    for part in safe_path_parts:
        folder = folder / part

    filename = rojo_filename(name, script_type)
    return {
        "path": folder / filename,
        "root": root,
        "service": service,
        "segments": safe_path_parts,
        "name": sanitize_segment(name, "name"),
        "type": script_type,
    }


class EventLog:
    def __init__(self):
        self._lock = threading.Lock()
        self._next_sequence = 1
        self._events = []

    def append(self, event):
        with self._lock:
            entry = dict(event)
            entry["sequence"] = self._next_sequence
            self._next_sequence += 1
            self._events.append(entry)
            if len(self._events) > 2000:
                self._events = self._events[-1000:]
            return entry["sequence"]

    def after(self, sequence):
        with self._lock:
            return [event for event in self._events if event["sequence"] > sequence]


class FileWatcher:
    def __init__(self, root, event_log):
        self.root = root
        self.event_log = event_log
        self.lock = threading.Lock()
        self.snapshot = {}
        self.recent_writes = {}
        self.thread = None
        self.observer = None
        self.pending_poll = False

    def start(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.snapshot = self._scan()
        if WATCHDOG_AVAILABLE:
            self.observer = Observer()
            self.observer.schedule(WatchdogBridge(self), str(self.root), recursive=True)
            self.observer.start()
            print("[Watcher] Using watchdog filesystem events")
        else:
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()
            print("[Watcher] watchdog not installed, using 1s polling fallback")

    def stop(self):
        if self.observer is not None:
            self.observer.stop()
            self.observer.join(timeout=2)

    def note_server_write(self, path, source_hash):
        with self.lock:
            self.recent_writes[str(path.resolve())] = {
                "hash": source_hash,
                "until": time.time() + WRITE_SUPPRESSION_SECONDS,
            }

    def schedule_poll(self, delay=0.15):
        with self.lock:
            if self.pending_poll:
                return
            self.pending_poll = True

        timer = threading.Timer(delay, self._run_scheduled_poll)
        timer.daemon = True
        timer.start()

    def _run_scheduled_poll(self):
        try:
            self._poll_once()
        except Exception as exc:
            print(f"[ERROR] watcher event poll failed: {exc}")
        finally:
            with self.lock:
                self.pending_poll = False

    def _run(self):
        while True:
            try:
                self._poll_once()
            except Exception as exc:
                print(f"[ERROR] watcher poll failed: {exc}")
            time.sleep(POLL_INTERVAL_SECONDS)

    def _poll_once(self):
        current = self._scan()
        previous = self.snapshot

        removed_paths = set(previous) - set(current)
        added_or_changed = []

        for rel_file, metadata in current.items():
            old = previous.get(rel_file)
            if old is None or old["hash"] != metadata["hash"]:
                added_or_changed.append((rel_file, metadata))

        for rel_file in sorted(removed_paths):
            removed = previous[rel_file]
            if self._should_suppress(removed["abs_path"], "__deleted__"):
                continue

            event = removed["entry"].copy()
            event.update(
                {
                    "op": "delete",
                    "relFile": rel_file,
                    "source": "disk",
                }
            )
            self.event_log.append(event)
            print(f"[Disk->Studio] Deleted {rel_file}")

        for rel_file, metadata in sorted(added_or_changed, key=lambda item: item[0]):
            if self._should_suppress(metadata["abs_path"], metadata["hash"]):
                continue

            op = "update" if rel_file in previous else "upsert"
            event = metadata["entry"].copy()
            event.update(
                {
                    "op": op,
                    "relFile": rel_file,
                    "source": "disk",
                }
            )
            self.event_log.append(event)
            print(f"[Disk->Studio] {op} {rel_file}")

        self.snapshot = current

    def _should_suppress(self, abs_path, source_hash):
        key = str(abs_path.resolve())
        now = time.time()
        with self.lock:
            marker = self.recent_writes.get(key)
            expired = [
                item_key
                for item_key, item_value in self.recent_writes.items()
                if item_value["until"] < now
            ]
            for item_key in expired:
                self.recent_writes.pop(item_key, None)

            if not marker:
                return False
            if marker["until"] < now:
                self.recent_writes.pop(key, None)
                return False
            if marker["hash"] == source_hash:
                self.recent_writes.pop(key, None)
                return True
            return False

    def _scan(self):
        snapshot = {}
        if not self.root.exists():
            return snapshot

        for path in self.root.rglob("*.luau"):
            if not path.is_file():
                continue

            script_type, script_name = infer_script_type(path.name)
            if not script_type:
                continue

            try:
                rel = path.relative_to(self.root)
            except ValueError:
                continue

            parts = list(rel.parts)
            if len(parts) < 2:
                continue

            service = parts[0]
            folder_parts = parts[1:-1]

            try:
                sanitize_segment(service, "service")
                for part in folder_parts:
                    sanitize_segment(part, "path[]")
                sanitize_segment(script_name, "name")
            except ValueError:
                continue

            try:
                source = path.read_text(encoding="utf-8")
            except OSError:
                continue

            rel_file = safe_relpath(path, self.root)
            snapshot[rel_file] = {
                "hash": hash_text(source),
                "abs_path": path.resolve(),
                "entry": {
                    "service": service,
                    "path": folder_parts,
                    "name": script_name,
                    "type": script_type,
                },
            }

        return snapshot


EVENT_LOG = EventLog()
WATCHER = FileWatcher(project_src_root(), EVENT_LOG)


class ExportHandler(http.server.BaseHTTPRequestHandler):
    def _send_json(self, status_code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Invalid Content-Length")

        raw_body = self.rfile.read(length)
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("Request body must be valid JSON")

        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def do_POST(self):
        try:
            if self.path == SYNC_ROUTE:
                self._handle_sync()
                return
            self._send_json(404, {"error": "Not found"})
        except Exception as exc:  # Defensive: keep server alive.
            print(f"[ERROR] POST {self.path} failed: {exc}")
            try:
                self._send_json(500, {"error": "Internal server error"})
            except Exception:
                pass

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            if parsed.path == MANIFEST_ROUTE:
                self._handle_manifest(parsed)
                return
            if parsed.path == FILE_ROUTE:
                self._handle_file(parsed)
                return
            if parsed.path == EVENTS_ROUTE:
                self._handle_events(parsed)
                return
            self._send_json(404, {"error": "Not found"})
        except Exception as exc:  # Defensive: keep server alive.
            print(f"[ERROR] GET {self.path} failed: {exc}")
            try:
                self._send_json(500, {"error": "Internal server error"})
            except Exception:
                pass

    def _handle_sync(self):
        payload = self._read_json_body()
        project_root = payload.get("projectRoot")
        if project_root != PROJECT_ROOT:
            self._send_json(400, {"error": "Invalid projectRoot"})
            return

        upserts = payload.get("upserts", payload.get("scripts", []))
        deletes = payload.get("deletes", [])
        if not isinstance(upserts, list):
            self._send_json(400, {"error": "upserts must be an array"})
            return
        if not isinstance(deletes, list):
            self._send_json(400, {"error": "deletes must be an array"})
            return

        written = 0
        deleted = 0
        errors = []

        for i, record in enumerate(upserts):
            if not isinstance(record, dict):
                errors.append(f"upserts[{i}] must be an object")
                continue

            source = record.get("source")
            if not isinstance(source, str):
                errors.append(f"upserts[{i}]: source must be a string")
                continue

            try:
                target = build_target(record)
                target["path"].parent.mkdir(parents=True, exist_ok=True)
                with target["path"].open("w", encoding="utf-8", newline="") as file_handle:
                    file_handle.write(source)

                WATCHER.note_server_write(target["path"], hash_text(source))
                written += 1
            except ValueError as exc:
                errors.append(f"upserts[{i}]: {exc}")

        for i, record in enumerate(deletes):
            if not isinstance(record, dict):
                errors.append(f"deletes[{i}] must be an object")
                continue

            try:
                target = build_target(record)
                target_path = target["path"]
                if target_path.exists():
                    target_path.unlink()
                    self._remove_empty_parents(target_path.parent, project_src_root())
                WATCHER.note_server_write(target_path, "__deleted__")
                deleted += 1
            except ValueError as exc:
                errors.append(f"deletes[{i}]: {exc}")

        print(f"[Studio->Disk] Upserts={written} Deletes={deleted}")
        if errors:
            print(f"[Studio->Disk] Skipped {len(errors)} invalid entries")

        self._send_json(
            200,
            {
                "written": written,
                "deleted": deleted,
                "skipped": len(errors),
                "errors": errors,
            },
        )

    def _remove_empty_parents(self, path, stop_root):
        current = path
        stop_root = stop_root.resolve()
        while True:
            try:
                if current.resolve() == stop_root:
                    return
            except OSError:
                return

            try:
                current.rmdir()
            except OSError:
                return

            parent = current.parent
            if parent == current:
                return
            current = parent

    def _handle_manifest(self, parsed):
        params = parse_qs(parsed.query)
        project_root = params.get("projectRoot", [None])[0]
        if project_root != PROJECT_ROOT:
            self._send_json(400, {"error": "Invalid projectRoot"})
            return

        root = project_src_root()
        if not root.exists():
            self._send_json(404, {"error": "Project src folder not found"})
            return

        entries = []
        skipped = 0

        for path in root.rglob("*.luau"):
            if not path.is_file():
                continue
            script_type, script_name = infer_script_type(path.name)
            if not script_type:
                continue

            try:
                rel = path.relative_to(root)
            except ValueError:
                skipped += 1
                continue

            parts = list(rel.parts)
            if len(parts) < 2:
                skipped += 1
                continue

            service = parts[0]
            folder_parts = parts[1:-1]

            try:
                sanitize_segment(service, "service")
                for part in folder_parts:
                    sanitize_segment(part, "path[]")
                sanitize_segment(script_name, "name")
            except ValueError:
                skipped += 1
                continue

            entries.append(
                {
                    "service": service,
                    "path": folder_parts,
                    "name": script_name,
                    "type": script_type,
                    "relFile": safe_relpath(path, root),
                }
            )

        if skipped:
            print(f"Manifest skipped {skipped} invalid paths")
        self._send_json(200, entries)

    def _handle_file(self, parsed):
        params = parse_qs(parsed.query)
        project_root = params.get("projectRoot", [None])[0]
        if project_root != PROJECT_ROOT:
            self._send_json(400, {"error": "Invalid projectRoot"})
            return

        rel_file = params.get("relFile", [None])[0]
        if not rel_file or not isinstance(rel_file, str):
            self._send_json(400, {"error": "Missing relFile"})
            return

        rel_file = unquote(rel_file)
        if rel_file.startswith("/") or rel_file.startswith("\\"):
            self._send_json(400, {"error": "Invalid relFile"})
            return

        root = project_src_root()
        target = (root / rel_file).resolve()
        if not is_within_root(target, root.resolve()):
            self._send_json(400, {"error": "Invalid relFile"})
            return
        if not target.exists() or not target.is_file():
            self._send_json(404, {"error": "File not found"})
            return

        try:
            source = target.read_text(encoding="utf-8")
        except OSError:
            self._send_json(500, {"error": "Failed to read file"})
            return

        self._send_json(200, {"source": source})

    def _handle_events(self, parsed):
        params = parse_qs(parsed.query)
        project_root = params.get("projectRoot", [None])[0]
        if project_root != PROJECT_ROOT:
            self._send_json(400, {"error": "Invalid projectRoot"})
            return

        raw_since = params.get("since", ["0"])[0]
        try:
            since = int(raw_since)
        except ValueError:
            self._send_json(400, {"error": "since must be an integer"})
            return

        events = EVENT_LOG.after(since)
        self._send_json(
            200,
            {
                "events": events,
                "nextSequence": events[-1]["sequence"] if events else since,
            },
        )

    def log_message(self, fmt, *args):
        print(f"{self.client_address[0]} - {fmt % args}")


def main():
    WATCHER.start()
    server = http.server.ThreadingHTTPServer((HOST, PORT), ExportHandler)
    print(f"Listening on http://{HOST}:{PORT}{SYNC_ROUTE}")
    print(f"Watching project files under: {project_src_root()}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
    finally:
        server.server_close()
        WATCHER.stop()


class WatchdogBridge(FileSystemEventHandler):
    def __init__(self, watcher):
        super().__init__()
        self.watcher = watcher

    def on_any_event(self, event):
        if getattr(event, "is_directory", False):
            return

        src_path = str(getattr(event, "src_path", "") or "")
        dest_path = str(getattr(event, "dest_path", "") or "")
        if not src_path.endswith(".luau") and not dest_path.endswith(".luau"):
            return

        self.watcher.schedule_poll()


if __name__ == "__main__":
    main()
