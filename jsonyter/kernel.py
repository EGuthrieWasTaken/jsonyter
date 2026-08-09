"""WebSocket connection to a running kernel via the Jupyter server.

A background pump thread owns the socket and is the only caller of
``recv()``. It routes each message either to the request that is waiting for
it (matched on ``parent_header.msg_id``) or to registered listeners, which is
what makes three things possible at once: ``execute`` can report output while
it is still running, other threads can talk to the kernel (or interrupt it)
while an execution is in flight, and kernel status transitions are observable
even when nothing is executing.

``execute`` and friends still block until the kernel replies and still return
plain JSON-serializable dicts, so existing callers are unaffected.
"""

import json
import queue
import threading

import websocket

from . import messages
from .client import JupyterError, prettifiable


class KernelConnection:
    """A connection to one kernel's ``/api/kernels/<id>/channels`` socket."""

    def __init__(self, client, kernel_id, session_id=None):
        self.client = client
        self.kernel_id = kernel_id
        self.session_id = session_id or messages.new_id()
        self._ws = None
        self._pending = {}          # msg_id -> Queue of kernel messages
        self._listeners = []        # callables receiving event dicts
        self._send_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._pump = None
        self._closing = False
        self.execution_state = None

    # ------------------------------------------------------------ connection

    @property
    def ws_url(self):
        base = self.client.base_url
        scheme = "wss" if base.startswith("https") else "ws"
        host = base.split("://", 1)[1]
        return "{}://{}/api/kernels/{}/channels?session_id={}".format(
            scheme, host, self.kernel_id, self.session_id)

    @property
    def connected(self):
        return self._ws is not None and self._ws.connected

    def connect(self):
        if self.connected:
            return self
        headers = []
        if self.client.token:
            headers.append("Authorization: token " + self.client.token)
        try:
            self._ws = websocket.create_connection(
                self.ws_url, header=headers,
                timeout=self.client.timeout,
                sslopt=None if self.client._http.verify else
                {"cert_reqs": 0},
            )
        except (websocket.WebSocketException, OSError) as exc:
            raise JupyterError(str(exc), url=self.ws_url) from exc
        # The handshake used client.timeout; the pump must block instead.
        self._ws.settimeout(None)
        self._closing = False
        self._pump = threading.Thread(
            target=self._pump_loop, name="jsonyter-pump-" + self.kernel_id[:8],
            daemon=True)
        self._pump.start()
        return self

    def close(self):
        self._closing = True
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        pump, self._pump = self._pump, None
        if pump is not None and pump is not threading.current_thread():
            pump.join(timeout=2.0)
        # Wake anything still waiting on a reply.
        self._fail_pending("connection closed")
        return {"id": self.kernel_id, "closed": True}

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc_info):
        self.close()

    # ------------------------------------------------------------------ pump

    def _pump_loop(self):
        while True:
            try:
                raw = self._ws.recv()
            except Exception as exc:            # closed, reset, protocol error
                self._disconnected(str(exc))
                return
            if raw is None or raw == "":
                # A clean close surfaces as an empty frame rather than an
                # exception; without this check the loop would spin.
                ws = self._ws
                if ws is None or not ws.connected:
                    self._disconnected("connection closed by server")
                    return
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            self._route(msg)

    def _disconnected(self, reason):
        self.execution_state = None
        if self._closing:
            return                              # we asked for it
        self._emit_event({"type": "disconnected", "message": reason,
                          "kernel_id": self.kernel_id})
        self._fail_pending("connection lost: {}".format(reason))

    def _route(self, msg):
        msg_type = msg.get("header", {}).get("msg_type")
        content = msg.get("content", {})

        # Status and shutdown are broadcast: listeners see them even when the
        # message also belongs to an in-flight request, so kernel state stays
        # observable. A server may keep the socket open briefly after the
        # kernel goes away, so shutdown_reply is the timely death signal.
        if msg_type == "status":
            state = content.get("execution_state")
            self.execution_state = state
            self._emit_event({"type": "status", "execution_state": state,
                              "kernel_id": self.kernel_id})
        elif msg_type == "shutdown_reply":
            self._emit_event({"type": "dead", "kernel_id": self.kernel_id,
                              "restart": content.get("restart", False)})

        parent = msg.get("parent_header", {}).get("msg_id")
        with self._state_lock:
            waiter = self._pending.get(parent)
        if waiter is not None:
            waiter.put(msg)
        elif msg_type not in ("status", "shutdown_reply"):   # already broadcast
            # Unsolicited traffic — e.g. output from another client attached
            # to the same kernel.
            self._emit_event({"type": msg_type, "content": content,
                              "kernel_id": self.kernel_id})

    def _fail_pending(self, reason):
        with self._state_lock:
            waiters = list(self._pending.values())
        for waiter in waiters:
            waiter.put({"__jsonyter_error__": reason})

    # -------------------------------------------------------------- listeners

    def add_listener(self, callback):
        """Register ``callback(event)`` for async kernel events.

        Events are dicts: ``{"type": "status", "execution_state": "busy"|
        "idle"|"starting", "kernel_id": ...}`` for state transitions and
        ``{"type": "disconnected", "message": ...}`` if the socket drops
        (which is how a died/restarted kernel surfaces). Callbacks run on the
        pump thread, so they must not block.
        """
        self._listeners.append(callback)
        return callback

    def remove_listener(self, callback):
        try:
            self._listeners.remove(callback)
        except ValueError:
            pass
        return callback

    def _emit_event(self, event):
        for callback in list(self._listeners):
            try:
                callback(event)
            except Exception:
                pass  # a bad listener must not kill the pump

    # ------------------------------------------------------------- messaging

    def _send(self, msg):
        self.connect()
        with self._send_lock:
            self._ws.send(json.dumps(msg))
        return msg["header"]["msg_id"]

    def _register(self, msg_id):
        waiter = queue.Queue()
        with self._state_lock:
            self._pending[msg_id] = waiter
        return waiter

    def _unregister(self, msg_id):
        with self._state_lock:
            self._pending.pop(msg_id, None)

    def _await(self, waiter, timeout):
        """Next message for a request, or raise on timeout/disconnect."""
        try:
            msg = waiter.get(timeout=timeout)
        except queue.Empty:
            raise JupyterError(
                "timed out after {}s waiting for a kernel reply "
                "(no message arrived in that window; pass a larger "
                "timeout=, or timeout=None to wait indefinitely — some "
                "kernels, e.g. SAS, are slow to respond on a fresh "
                "connection)".format(timeout),
                url=self.ws_url,
            ) from None
        if "__jsonyter_error__" in msg:
            raise JupyterError(msg["__jsonyter_error__"], url=self.ws_url)
        return msg

    def _resolve_timeout(self, timeout):
        return timeout if timeout is not None else self.client.exec_timeout

    def _request_reply(self, msg, timeout=None):
        """Send ``msg`` and return the matching ``*_reply`` content."""
        timeout = self._resolve_timeout(timeout)
        msg_id = msg["header"]["msg_id"]
        reply_type = msg["header"]["msg_type"].replace("_request", "_reply")
        waiter = self._register(msg_id)
        try:
            self._send(msg)
            while True:
                received = self._await(waiter, timeout)
                if received["header"]["msg_type"] == reply_type:
                    return received["content"]
        finally:
            self._unregister(msg_id)

    # --------------------------------------------------------------- execute

    @prettifiable
    def execute(self, code, timeout=None, silent=False, store_history=True,
                stdin_callback=None, on_output=None):
        """Run ``code`` and collect everything the kernel says about it.

        Returns a dict shaped for direct rendering by a REPL front end::

            {"status": "ok" | "error" | "aborted",
             "execution_count": 3,
             "outputs": [
               {"type": "stream", "name": "stdout", "text": "..."},
               {"type": "execute_result", "data": {"text/plain": "2"},
                "metadata": {}, "execution_count": 3},
               {"type": "display_data", "data": {...}, "metadata": {}},
               {"type": "error", "ename": "...", "evalue": "...",
                "traceback": ["..."]},
             ]}

        ``on_output``, if given, is called with each output dict the moment it
        arrives, before the call returns — that is how a front end shows a
        long-running cell's ``print`` output as it is produced instead of only
        at the end. The same dicts still appear in the final ``outputs``.

        ``stdin_callback``, if given, is called with the kernel's
        ``input_request`` content (``{"prompt": ..., "password": ...}``) and
        must return the string to send back — this is how ``input()`` works
        from a connected REPL.

        ``timeout`` bounds how long to wait with no message from the kernel
        (see :class:`Client` for the default and rationale); it defaults to
        ``client.exec_timeout``, which is ``None`` (wait indefinitely) unless
        you configured otherwise. Because the socket is pumped by a background
        thread, another thread may call ``interrupt_kernel`` while this call
        is blocked — that is the intended way to stop a runaway cell.
        """
        timeout = self._resolve_timeout(timeout)
        msg = messages.execute_request(
            self.session_id, code, silent=silent, store_history=store_history,
            allow_stdin=stdin_callback is not None,
        )
        msg_id = msg["header"]["msg_id"]

        outputs = []
        result = {"status": None, "execution_count": None, "outputs": outputs}
        got_reply = False
        got_idle = False

        def collect(output):
            outputs.append(output)
            if on_output is not None:
                on_output(output)

        waiter = self._register(msg_id)
        try:
            self._send(msg)
            while not (got_reply and got_idle):
                received = self._await(waiter, timeout)
                msg_type = received["header"]["msg_type"]
                content = received["content"]

                if msg_type == "execute_reply":
                    result["status"] = content.get("status")
                    result["execution_count"] = content.get("execution_count")
                    got_reply = True
                elif msg_type == "status":
                    if content.get("execution_state") == "idle":
                        got_idle = True
                elif msg_type == "stream":
                    collect({"type": "stream",
                             "name": content.get("name"),
                             "text": content.get("text")})
                elif msg_type in ("execute_result", "display_data",
                                  "update_display_data"):
                    output = {"type": msg_type,
                              "data": content.get("data", {}),
                              "metadata": content.get("metadata", {})}
                    if msg_type == "execute_result":
                        output["execution_count"] = content.get(
                            "execution_count")
                    collect(output)
                elif msg_type == "error":
                    collect({"type": "error",
                             "ename": content.get("ename"),
                             "evalue": content.get("evalue"),
                             "traceback": content.get("traceback", [])})
                elif msg_type == "input_request":
                    if stdin_callback is None:
                        # Should not happen with allow_stdin=False, but never
                        # leave the kernel hanging on a read.
                        self._send(messages.input_reply(self.session_id, ""))
                    else:
                        self._send(messages.input_reply(
                            self.session_id, stdin_callback(content)))
                elif msg_type == "clear_output":
                    collect({"type": "clear_output",
                             "wait": content.get("wait", False)})
        finally:
            self._unregister(msg_id)

        return result

    # ---------------------------------------------------------- introspection

    @prettifiable
    def complete(self, code, cursor_pos=None, timeout=None):
        """Completion candidates at ``cursor_pos`` (default: end of code)."""
        return self._request_reply(
            messages.complete_request(self.session_id, code, cursor_pos),
            timeout)

    @prettifiable
    def inspect(self, code, cursor_pos=None, detail_level=0, timeout=None):
        """Documentation/introspection for the object at ``cursor_pos``."""
        return self._request_reply(
            messages.inspect_request(self.session_id, code, cursor_pos,
                                     detail_level),
            timeout)

    @prettifiable
    def is_complete(self, code, timeout=None):
        """Whether ``code`` is complete input (drives REPL Enter behavior)."""
        return self._request_reply(
            messages.is_complete_request(self.session_id, code), timeout)

    @prettifiable
    def kernel_info(self, timeout=None):
        return self._request_reply(
            messages.kernel_info_request(self.session_id), timeout)

    @prettifiable
    def history(self, n=50, timeout=None, **kwargs):
        return self._request_reply(
            messages.history_request(self.session_id, n=n, **kwargs), timeout)
