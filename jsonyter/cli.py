"""JSON-over-stdio interface, designed to be driven by Emacs.

Run ``jsonyter --url http://localhost:8888 --token SECRET`` and write one
JSON request per line to stdin; one JSON response per line comes back on
stdout. Requests look like::

    {"id": 1, "method": "execute", "params": {"kernel_id": "...", "code": "1+1"}}

and responses like::

    {"id": 1, "result": {...}}
    {"id": 1, "error": {"error": "JupyterError", "message": "...", ...}}

From Emacs, start the process with ``make-process`` and parse each line with
``json-parse-string`` — see the README for a working elisp sketch.

Available methods (params in parentheses):

- ``status``, ``version``, ``list_kernelspecs``, ``list_kernels``,
  ``list_sessions``
- ``start_kernel`` (``name``), ``get_kernel``/``shutdown_kernel``/
  ``restart_kernel``/``interrupt_kernel`` (``kernel_id``)
- ``create_session`` (``path``, ``kernel_name``), ``delete_session`` (``session_id``)
- ``execute`` (``kernel_id``, ``code``, ``timeout``, ``silent``)
- ``complete``/``inspect`` (``kernel_id``, ``code``, ``cursor_pos``),
  ``is_complete`` (``kernel_id``, ``code``), ``kernel_info`` (``kernel_id``)
- ``disconnect`` (``kernel_id``) — close the websocket but leave the kernel up

An ``execute`` that hits ``input()`` in the kernel emits an out-of-band line
``{"id": ..., "input_request": {"prompt": ...}}``; answer it by writing
``{"input": "the users answer"}`` on the next line.
"""

import argparse
import json
import sys

from .client import Client, JupyterError

# Client methods invocable directly, mapped to required/optional params.
_CLIENT_METHODS = {
    "status": (), "version": (), "list_kernelspecs": (), "list_kernels": (),
    "list_sessions": (),
    "start_kernel": ("name",),
    "get_kernel": ("kernel_id",),
    "shutdown_kernel": ("kernel_id",),
    "restart_kernel": ("kernel_id",),
    "interrupt_kernel": ("kernel_id",),
    "create_session": ("path", "kernel_name", "session_type", "name"),
    "get_session": ("session_id",),
    "delete_session": ("session_id",),
    "get_contents": ("path", "content"),
}

_KERNEL_METHODS = {
    "execute": ("code", "timeout", "silent", "store_history"),
    "complete": ("code", "cursor_pos", "timeout"),
    "inspect": ("code", "cursor_pos", "detail_level", "timeout"),
    "is_complete": ("code", "timeout"),
    "kernel_info": ("timeout",),
    "history": ("n", "timeout"),
}


class Dispatcher:
    def __init__(self, client, stdin=None, stdout=None):
        self.client = client
        self.connections = {}
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout

    def _emit(self, obj):
        self.stdout.write(json.dumps(obj) + "\n")
        self.stdout.flush()

    def _connection(self, kernel_id):
        if kernel_id not in self.connections:
            self.connections[kernel_id] = self.client.kernel(kernel_id)
        return self.connections[kernel_id]

    def _stdin_callback(self, request_id):
        def ask(content):
            self._emit({"id": request_id, "input_request": content})
            line = self.stdin.readline()
            if not line:
                return ""
            try:
                return json.loads(line).get("input", "")
            except ValueError:
                return line.rstrip("\n")
        return ask

    def dispatch(self, request):
        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}

        if method in _CLIENT_METHODS:
            allowed = _CLIENT_METHODS[method]
            kwargs = {k: v for k, v in params.items() if k in allowed}
            args = [kwargs.pop("kernel_id")] if "kernel_id" in kwargs else []
            if "session_id" in kwargs:
                args = [kwargs.pop("session_id")]
            if method == "shutdown_kernel" and args:
                self.connections.pop(args[0], None)
            result = getattr(self.client, method)(*args, **kwargs)
        elif method in _KERNEL_METHODS:
            kernel_id = params.get("kernel_id")
            if not kernel_id:
                raise JupyterError("missing required param: kernel_id")
            conn = self._connection(kernel_id)
            kwargs = {k: v for k, v in params.items()
                      if k in _KERNEL_METHODS[method]}
            if method == "execute":
                kwargs["stdin_callback"] = self._stdin_callback(request_id)
            result = getattr(conn, method)(**kwargs)
        elif method == "disconnect":
            conn = self.connections.pop(params.get("kernel_id"), None)
            result = conn.close() if conn else {"closed": False}
        elif method == "methods":
            result = (sorted(_CLIENT_METHODS) + sorted(_KERNEL_METHODS)
                      + ["disconnect", "methods"])
        else:
            raise JupyterError("unknown method: {!r}".format(method))
        return {"id": request_id, "result": result}

    def run(self):
        for line in self.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except ValueError as exc:
                self._emit({"id": None, "error": {
                    "error": "ParseError", "message": str(exc)}})
                continue
            try:
                self._emit(self.dispatch(request))
            except JupyterError as exc:
                self._emit({"id": request.get("id"),
                            "error": exc.to_json()})
            except Exception as exc:  # keep the pipe alive on bugs
                self._emit({"id": request.get("id"), "error": {
                    "error": type(exc).__name__, "message": str(exc)}})


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="jsonyter",
        description="JSON-over-stdio bridge to a Jupyter server.")
    parser.add_argument("--url", default="http://localhost:8888",
                        help="Jupyter server base URL")
    parser.add_argument("--token", default=None,
                        help="Jupyter auth token (or set in the URL provider)")
    parser.add_argument("--timeout", type=float, default=30.0,
                        help="default request timeout in seconds")
    parser.add_argument("--insecure", action="store_true",
                        help="skip TLS certificate verification")
    args = parser.parse_args(argv)

    client = Client(args.url, token=args.token, timeout=args.timeout,
                    verify_tls=not args.insecure)
    dispatcher = Dispatcher(client)
    try:
        dispatcher.run()
    except KeyboardInterrupt:
        pass
    finally:
        for conn in dispatcher.connections.values():
            try:
                conn.close()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
