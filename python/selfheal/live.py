"""Authenticated loopback control for an explicitly integrated running app."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import secrets
from threading import Lock, Thread
from types import ModuleType

from .models import HealthReport, RepairEvent
from .runtime import SelfHealRuntime


@dataclass(frozen=True, slots=True)
class LiveTarget:
    name: str
    module_name: str
    symbol_name: str
    health_check: Callable[[ModuleType], HealthReport | Awaitable[HealthReport]]
    instructions: str = ""
    editable_symbols: tuple[str, ...] = ()
    retry_safe: bool = False

    def __post_init__(self):
        if not self.name or not self.module_name or not self.symbol_name or not callable(self.health_check):
            raise ValueError("A target needs a name, module, symbol and developer health check")
        if type(self.retry_safe) is not bool:
            raise ValueError("retry_safe must be a boolean")
        if type(self.instructions) is not str or len(self.instructions.encode("utf-8")) > 4000:
            raise ValueError("Target instructions must be a string of at most 4000 bytes")
        if not self.editable_symbols:
            object.__setattr__(self, "editable_symbols", (self.symbol_name,))
        if not isinstance(self.editable_symbols, tuple) or len(set(self.editable_symbols)) != len(self.editable_symbols) or any(
            type(name) is not str or not name.isidentifier() for name in self.editable_symbols
        ):
            raise ValueError("editable_symbols must contain unique function names")
        if self.symbol_name not in self.editable_symbols:
            raise ValueError("editable_symbols must include the registered symbol_name")


class LiveControlServer:
    """Only registered targets can change; commands never supply paths or code.

    Route application calls through runtime.invoke/call to participate in the
    activation lock. The session file is a local credential, not a public URL.
    """

    def __init__(self, runtime: SelfHealRuntime, targets: tuple[LiveTarget, ...], *,
                 port: int = 8766, session_file: Path = Path(".selfheal/live.json"), agent=None):
        if not targets or len({target.name for target in targets}) != len(targets):
            raise ValueError("Register at least one target with unique names")
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        for target in targets:
            if not target.name or not callable(target.health_check):
                raise ValueError("Every target needs a name and developer health check")
            source = runtime.language.source_file(target.module_name)
            source.relative_to(runtime.project_root)
        self.runtime = runtime
        if agent is not None and (agent.runtime is not runtime or agent.targets != {target.name: target for target in targets}):
            raise ValueError("The control server and agent must share runtime and registered targets")
        self.agent = agent
        self.targets = {target.name: target for target in targets}
        self.session_file = session_file.resolve()
        self._token = secrets.token_urlsafe(32)
        self._operation = Lock()
        self._last = None
        self._thread = None
        self._session_owned = False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self):
                super().setup()
                self.connection.settimeout(10)

            def log_message(self, *args):
                pass  # Never log headers or local session credentials.

            def reply(self, status, data):
                self.close_connection = True
                payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(payload)

            def authenticated(self):
                expected_host = f"127.0.0.1:{owner.port}"
                if self.headers.get("Origin") or self.headers.get("Host") != expected_host:
                    self.reply(403, {"error": "Loopback command clients only"})
                    return False
                value = self.headers.get("Authorization", "")
                if not hmac.compare_digest(value.encode(), ("Bearer " + owner._token).encode()):
                    self.reply(401, {"error": "Invalid local session credential"})
                    return False
                return True

            def do_GET(self):
                if not self.authenticated():
                    return
                if self.path != "/status":
                    self.reply(404, {"error": "Unknown endpoint"})
                    return
                self.reply(200, owner.status())

            def do_POST(self):
                if not self.authenticated():
                    return
                if self.path == "/agent":
                    self.agent_command()
                    return
                if self.path != "/command":
                    self.reply(404, {"error": "Unknown endpoint"})
                    return
                try:
                    if self.headers.get("Transfer-Encoding") or self.headers.get_content_type() != "application/json":
                        raise ValueError
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 16_000:
                        raise ValueError
                    body = json.loads(self.rfile.read(size))
                    if not isinstance(body, dict) or set(body) != {"operation", "target", "instructions", "preview"}:
                        raise ValueError
                    operation, name = body["operation"], body["target"]
                    if operation not in ("heal", "improve") or type(name) is not str or name not in owner.targets:
                        raise ValueError
                    if type(body["preview"]) is not bool or type(body["instructions"]) is not str:
                        raise ValueError
                    if len(body["instructions"].encode("utf-8")) > 8000:
                        raise ValueError
                    if operation == "improve" and not body["instructions"].strip():
                        raise ValueError
                except (ValueError, TypeError, UnicodeError):
                    self.reply(400, {"error": "Invalid command or unregistered target"})
                    return
                if not owner._operation.acquire(blocking=False):
                    self.reply(409, {"error": "Another live inspection is active"})
                    return
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True
                    connected = True

                    def send(data):
                        nonlocal connected
                        if connected:
                            try:
                                self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8") + b"\n")
                                self.wfile.flush()
                            except OSError:
                                connected = False

                    def event_sink(event: RepairEvent):
                        send({"kind": "event", **asdict(event)})

                    target = owner.targets[name]
                    # An explicit heal/improve command authorizes activation; preview does not.
                    result = owner.runtime.inspect(
                        module_name=target.module_name, symbol_name=target.symbol_name,
                        health_check=target.health_check, retry_safe=target.retry_safe,
                        operation=operation,
                        instructions=target.instructions + "\n\n" + body["instructions"],
                        editable_symbols=target.editable_symbols,
                        apply=not body["preview"], on_event=event_sink,
                    )
                    owner._last = {"status": result.status, "healed": result.healed,
                                   "message": result.message, "proposal_id": result.proposal_id,
                                   "snapshot_file": result.snapshot_file}
                    send({"kind": "result", **owner._last, "pid": os.getpid()})
                except Exception as exc:
                    # No transport exception text: it may contain a credential.
                    owner._last = {"status": "failed", "healed": False,
                                   "message": f"Command stopped ({type(exc).__name__})"}
                    try:
                        send({"kind": "result", **owner._last, "pid": os.getpid()})
                    except UnboundLocalError:
                        pass
                finally:
                    owner._operation.release()

            def agent_command(self):
                if owner.agent is None:
                    self.reply(404, {"error": "No background agent registered"})
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if self.headers.get("Transfer-Encoding") or self.headers.get_content_type() != "application/json" or not 0 < size <= 2000:
                        raise ValueError
                    data = json.loads(self.rfile.read(size))
                    if not isinstance(data, dict) or set(data) != {"action", "target"}:
                        raise ValueError
                    action, name = data["action"], data["target"]
                    if type(action) is not str or action not in ("pause", "resume", "scan", "reset"):
                        raise ValueError
                    if name is not None and (type(name) is not str or name not in owner.targets):
                        raise ValueError
                    if action == "pause":
                        owner.agent.pause()
                    elif action == "resume":
                        owner.agent.resume()
                    elif action == "reset":
                        owner.agent.reset(name)
                    else:
                        owner.agent.request_scan(name)
                except (ValueError, TypeError, UnicodeError):
                    self.reply(400, {"error": "Invalid background agent command"})
                    return
                self.reply(200, owner.status())

        self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]

    def status(self):
        return {"pid": os.getpid(), "busy": self._operation.locked(),
                "healing_disabled": self.runtime.healing_disabled,
                "targets": list(self.targets), "last_result": self._last,
                "repair_timeout_seconds": getattr(getattr(self.runtime, "config", None), "repair_timeout_seconds", None),
                "hard_timeout": getattr(getattr(self.runtime, "config", None), "hard_timeout", None),
                "agent": self.agent.status() if self.agent is not None else None}

    def start(self):
        if self._thread is not None:
            raise RuntimeError("Control server already started")
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.session_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            self._server.server_close()
            raise RuntimeError(
                f"Session file exists: {self.session_file}. Stop its app first. "
                "If that app has exited, remove the stale session file or choose another --session."
            ) from None
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump({"pid": os.getpid(), "url": f"http://127.0.0.1:{self.port}",
                           "token": self._token}, file)
                file.flush()
                os.fsync(file.fileno())
            self._session_owned = True
            self._thread = Thread(target=self._server.serve_forever, name="selfheal-control", daemon=True)
            self._thread.start()
        except BaseException:
            self.session_file.unlink(missing_ok=True)
            self._server.server_close()
            raise
        return self

    def close(self):
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join(timeout=5)
        self._server.server_close()
        # Let an accepted repair commit or restore its snapshot before app exit.
        with self._operation:
            pass
        if self._session_owned:
            try:
                data = json.loads(self.session_file.read_text(encoding="utf-8"))
                if data.get("token") == self._token:
                    self.session_file.unlink()
            except (OSError, ValueError):
                pass
            self._session_owned = False

    def __enter__(self):
        return self.start()

    def __exit__(self, *args):
        self.close()
