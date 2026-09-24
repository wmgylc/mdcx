from __future__ import annotations

import hmac
import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

from PyQt6.QtCore import QObject, Qt, pyqtSignal, pyqtSlot

from .models.flags import Flags
from .signals import signal_qt

if TYPE_CHECKING:
    from .controllers.main_window.main_window import MyMAinWindow


_START_TIMEOUT_SECONDS = 5.0
_STOP_TIMEOUT_SECONDS = 5.0
_BUSY_STATES = {"starting", "running", "stopping"}


class _GuiRequest:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: dict[str, Any] = {}

    def finish(self, result: dict[str, Any]) -> None:
        self.result = result
        self.event.set()


class ScrapeControl(QObject):
    """Bridge HTTP worker threads to the GUI thread and expose scrape state."""

    _start_requested = pyqtSignal(object)
    _stop_requested = pyqtSignal(object)

    def __init__(self, window: "MyMAinWindow") -> None:
        super().__init__(window)
        self._window = window
        self._lock = threading.RLock()
        self._state = "running" if window.is_scrape_running() else "idle"
        self._run_id: str | None = str(uuid.uuid4()) if self._state == "running" else None
        self._started_at: float | None = time.time() if self._state == "running" else None
        self._last_error: str | None = None

        self._start_requested.connect(self._start_on_gui_thread, Qt.ConnectionType.QueuedConnection)
        self._stop_requested.connect(self._stop_on_gui_thread, Qt.ConnectionType.QueuedConnection)
        signal_qt.scrape_started.connect(self._on_scrape_started, Qt.ConnectionType.QueuedConnection)
        signal_qt.scrape_finished.connect(self._on_scrape_finished, Qt.ConnectionType.QueuedConnection)

    def request_start(self) -> dict[str, Any]:
        request = _GuiRequest()
        self._start_requested.emit(request)
        if not request.event.wait(_START_TIMEOUT_SECONDS):
            return {"accepted": False, "reason": "gui_timeout"}
        return request.result

    def request_stop(self) -> dict[str, Any]:
        request = _GuiRequest()
        self._stop_requested.emit(request)
        if not request.event.wait(_STOP_TIMEOUT_SECONDS):
            return {"accepted": False, "reason": "gui_timeout"}
        return request.result

    def status(self) -> dict[str, Any]:
        with self._lock:
            state = self._state
            run_id = self._run_id
            started_at = self._started_at
            last_error = self._last_error

        total = Flags.total_count
        done = Flags.scrape_done
        return {
            "state": state,
            "run_id": run_id,
            "file_mode": Flags.file_mode.name,
            "total": total,
            "done": done,
            "success": Flags.succ_count,
            "failed": Flags.fail_count,
            "remaining": max(total - done, 0),
            "stop_requested": Flags.stop_requested,
            "started_at": started_at,
            "last_error": last_error,
        }

    @pyqtSlot(object)
    def _start_on_gui_thread(self, request: _GuiRequest) -> None:
        try:
            with self._lock:
                if self._state in _BUSY_STATES:
                    request.finish(self._already_running_result())
                    return

                if self._window.is_scrape_running():
                    self._state = "running"
                    self._run_id = self._run_id or str(uuid.uuid4())
                    self._started_at = self._started_at or time.time()
                    request.finish(self._already_running_result())
                    return

                self._state = "starting"
                self._run_id = str(uuid.uuid4())
                self._started_at = time.time()
                self._last_error = None
                run_id = self._run_id

            future = self._window.start_scrape_from_control()
            if future is None:
                with self._lock:
                    self._state = "idle"
                    self._last_error = "start_failed"
                request.finish({"accepted": False, "reason": "start_failed", "status": self.status()})
                return

            with self._lock:
                self._state = "running"
            future.add_done_callback(lambda completed: self._future_finished(run_id, completed))
            request.finish({"accepted": True, "run_id": run_id, "status": self.status()})
        except Exception as error:
            with self._lock:
                self._state = "idle"
                self._last_error = str(error)
            request.finish({"accepted": False, "reason": "start_failed", "status": self.status()})

    def _already_running_result(self) -> dict[str, Any]:
        """Treat a duplicate start as a successful idempotent no-op."""
        return {
            "accepted": True,
            "idempotent": True,
            "reason": "already_running",
            "run_id": self._run_id,
            "status": self.status(),
        }

    @pyqtSlot(object)
    def _stop_on_gui_thread(self, request: _GuiRequest) -> None:
        try:
            with self._lock:
                if self._state not in _BUSY_STATES and not self._window.is_scrape_running():
                    request.finish({"accepted": False, "reason": "not_running", "status": self.status()})
                    return
                self._state = "stopping"

            if not self._window.stop_scrape_from_control():
                with self._lock:
                    self._state = "idle"
                request.finish({"accepted": False, "reason": "not_running", "status": self.status()})
                return

            request.finish({"accepted": True, "run_id": self._run_id, "status": self.status()})
        except Exception as error:
            with self._lock:
                self._state = "idle"
                self._last_error = str(error)
            request.finish({"accepted": False, "reason": "stop_failed", "status": self.status()})

    @pyqtSlot()
    def _on_scrape_started(self) -> None:
        with self._lock:
            if self._state not in _BUSY_STATES:
                self._state = "running"
                self._run_id = str(uuid.uuid4())
                self._started_at = time.time()
                self._last_error = None

    @pyqtSlot()
    def _on_scrape_finished(self) -> None:
        self._mark_finished()

    def _future_finished(self, run_id: str, future: Any) -> None:
        error: str | None = None
        try:
            future.result()
        except Exception as exc:
            error = str(exc)
        with self._lock:
            if self._run_id != run_id:
                return
            self._state = "idle"
            if error:
                self._last_error = error

    def _mark_finished(self) -> None:
        with self._lock:
            self._state = "idle"


class _ControlRequestHandler(BaseHTTPRequestHandler):
    server: "_ControlHTTPServer"

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(200, {"status": "ok"})
            return
        if self.path == "/api/scrape/status":
            if not self._authorized():
                return
            self._send_json(200, self.server.control.status())
            return
        self._send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        if self.path == "/api/scrape/start":
            if not self._authorized():
                return
            result = self.server.control.request_start()
            if result.get("idempotent"):
                response_status = 200
            elif result.get("accepted"):
                response_status = 202
            else:
                response_status = self._status_for_reason(result)
            self._send_json(response_status, result)
            return
        if self.path == "/api/scrape/stop":
            if not self._authorized():
                return
            result = self.server.control.request_stop()
            self._send_json(202 if result.get("accepted") else self._status_for_reason(result), result)
            return
        self._send_json(404, {"error": "not_found"})

    def _authorized(self) -> bool:
        token = self.server.token
        if not token:
            return True

        authorization = self.headers.get("Authorization", "")
        presented = authorization.removeprefix("Bearer ").strip()
        if not presented:
            presented = self.headers.get("X-MDCX-Control-Token", "").strip()
        if hmac.compare_digest(presented, token):
            return True
        self._send_json(401, {"error": "unauthorized"})
        return False

    @staticmethod
    def _status_for_reason(result: dict[str, Any]) -> int:
        if result.get("reason") == "already_running":
            return 409
        if result.get("reason") == "not_running":
            return 409
        if result.get("reason") == "gui_timeout":
            return 504
        return 500

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


class _ControlHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], control: ScrapeControl, token: str) -> None:
        super().__init__(address, _ControlRequestHandler)
        self.control = control
        self.token = token


class ScrapeControlServer:
    """Small HTTP control surface hosted by the GUI process itself."""

    def __init__(self, window: "MyMAinWindow") -> None:
        self.control = ScrapeControl(window)
        self.host = os.getenv("MDCX_CONTROL_HOST", "127.0.0.1")
        self.port = int(os.getenv("MDCX_CONTROL_PORT", "8750"))
        self.token = os.getenv("MDCX_CONTROL_TOKEN", "").strip()
        self._server: _ControlHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if os.getenv("MDCX_CONTROL_ENABLED", "1").strip().lower() in {"0", "false", "no"}:
            print("MDCx control API disabled by MDCX_CONTROL_ENABLED")
            return
        try:
            self._server = _ControlHTTPServer((self.host, self.port), self.control, self.token)
        except Exception as error:
            print(f"MDCx control API failed to bind {self.host}:{self.port}: {error}")
            return
        self._thread = threading.Thread(target=self._server.serve_forever, name="MDCxControlHTTP", daemon=True)
        self._thread.start()
        auth_state = "enabled" if self.token else "disabled"
        print(f"MDCx control API listening on http://{self.host}:{self.port} (auth={auth_state})")

    def stop(self) -> None:
        server = self._server
        if server is None:
            return
        server.shutdown()
        server.server_close()
        self._server = None
