"""WebSocket connection to a running kernel via the Jupyter server.

``execute`` and friends block until the kernel replies and return plain
JSON-serializable dicts summarizing the exchange, with the full output
stream (stdout/stderr, rich display data, errors) collected in order.
"""

import json

import websocket

from . import messages
from .client import JupyterError


class KernelConnection:
    """A connection to one kernel's ``/api/kernels/<id>/channels`` socket."""

    def __init__(self, client, kernel_id, session_id=None):
        self.client = client
        self.kernel_id = kernel_id
        self.session_id = session_id or messages.new_id()
        self._ws = None

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
        return self

    def close(self):
        if self._ws is not None:
            try:
                self._ws.close()
            finally:
                self._ws = None
        return {"id": self.kernel_id, "closed": True}

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc_info):
        self.close()

    # ------------------------------------------------------------- messaging

    def _send(self, msg):
        self.connect()
        self._ws.send(json.dumps(msg))
        return msg["header"]["msg_id"]

    def _recv(self, timeout):
        self._ws.settimeout(timeout)
        try:
            raw = self._ws.recv()
        except websocket.WebSocketTimeoutException as exc:
            raise JupyterError(
                "timed out waiting for kernel reply", url=self.ws_url,
            ) from exc
        return json.loads(raw)

    def _request_reply(self, msg, timeout=None):
        """Send ``msg`` and return the matching ``*_reply`` content."""
        timeout = timeout if timeout is not None else self.client.timeout
        msg_id = self._send(msg)
        reply_type = msg["header"]["msg_type"].replace("_request", "_reply")
        while True:
            received = self._recv(timeout)
            if (received.get("parent_header", {}).get("msg_id") == msg_id
                    and received["header"]["msg_type"] == reply_type):
                return received["content"]

    # --------------------------------------------------------------- execute

    def execute(self, code, timeout=None, silent=False, store_history=True,
                stdin_callback=None):
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

        ``stdin_callback``, if given, is called with the kernel's
        ``input_request`` content (``{"prompt": ..., "password": ...}``) and
        must return the string to send back — this is how ``input()`` works
        from a connected REPL.
        """
        timeout = timeout if timeout is not None else self.client.timeout
        msg = messages.execute_request(
            self.session_id, code, silent=silent, store_history=store_history,
            allow_stdin=stdin_callback is not None,
        )
        msg_id = self._send(msg)

        outputs = []
        result = {"status": None, "execution_count": None, "outputs": outputs}
        got_reply = False
        got_idle = False

        while not (got_reply and got_idle):
            received = self._recv(timeout)
            if received.get("parent_header", {}).get("msg_id") != msg_id:
                continue
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
                outputs.append({"type": "stream",
                                "name": content.get("name"),
                                "text": content.get("text")})
            elif msg_type in ("execute_result", "display_data",
                              "update_display_data"):
                output = {"type": msg_type,
                          "data": content.get("data", {}),
                          "metadata": content.get("metadata", {})}
                if msg_type == "execute_result":
                    output["execution_count"] = content.get("execution_count")
                outputs.append(output)
            elif msg_type == "error":
                outputs.append({"type": "error",
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
                outputs.append({"type": "clear_output",
                                "wait": content.get("wait", False)})

        return result

    # ---------------------------------------------------------- introspection

    def complete(self, code, cursor_pos=None, timeout=None):
        """Completion candidates at ``cursor_pos`` (default: end of code)."""
        return self._request_reply(
            messages.complete_request(self.session_id, code, cursor_pos),
            timeout)

    def inspect(self, code, cursor_pos=None, detail_level=0, timeout=None):
        """Documentation/introspection for the object at ``cursor_pos``."""
        return self._request_reply(
            messages.inspect_request(self.session_id, code, cursor_pos,
                                     detail_level),
            timeout)

    def is_complete(self, code, timeout=None):
        """Whether ``code`` is complete input (drives REPL Enter behavior)."""
        return self._request_reply(
            messages.is_complete_request(self.session_id, code), timeout)

    def kernel_info(self, timeout=None):
        return self._request_reply(
            messages.kernel_info_request(self.session_id), timeout)

    def history(self, n=50, timeout=None, **kwargs):
        return self._request_reply(
            messages.history_request(self.session_id, n=n, **kwargs), timeout)
