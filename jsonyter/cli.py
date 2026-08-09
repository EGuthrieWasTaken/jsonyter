"""JSON-over-stdio interface, designed to be driven by Emacs.

Run ``jsonyter --url http://localhost:8888`` and write one JSON request per
line to stdin; one JSON response per line comes back on stdout. Requests look
like::

    {"id": 1, "method": "execute", "params": {"kernel_id": "...", "code": "1+1"}}

and responses like::

    {"id": 1, "result": {...}}
    {"id": 1, "error": {"error": "JupyterError", "message": "...", ...}}

Requests are handled concurrently: calls that don't touch a kernel's socket
run immediately, and each kernel gets its own worker so a long ``execute``
never blocks the reader. That means an ``interrupt_kernel`` sent while code
is running is acted on right away, and responses may arrive out of request
order — match them by ``id``.

Lines that are not final responses are tagged by an extra key instead of
``result``/``error``, so a client can dispatch on which key is present:

- ``{"id": N, "output": {...}}`` — incremental output from a running
  ``execute`` (only when that request passed ``"stream": true``).
- ``{"id": N, "input_request": {"prompt": ...}}`` — the kernel wants stdin;
  reply with ``{"id": N, "input": "..."}`` (a bare ``{"input": "..."}`` also
  works when only one request is waiting).
- ``{"event": {...}, "kernel_id": ...}`` — async kernel state, after
  ``subscribe``.

Available methods (params in parentheses):

- ``status``, ``version``, ``list_kernelspecs``, ``list_kernels``,
  ``list_sessions``
- ``start_kernel`` (``name``), ``get_kernel``/``shutdown_kernel``/
  ``restart_kernel``/``interrupt_kernel`` (``kernel_id``)
- ``create_session`` (``path``, ``kernel_name``), ``delete_session`` (``session_id``)
- ``execute`` (``kernel_id``, ``code``, ``timeout``, ``silent``, ``stream``)
- ``complete``/``inspect`` (``kernel_id``, ``code``, ``cursor_pos``),
  ``is_complete`` (``kernel_id``, ``code``), ``kernel_info`` (``kernel_id``)
- ``subscribe``/``unsubscribe`` (``kernel_id``) — async kernel status events
- ``disconnect`` (``kernel_id``) — close the websocket but leave the kernel up
"""

import argparse
import json
import os
import queue
import sys
import threading

from .client import Client, JupyterError

# Client methods invocable directly, mapped to accepted params.
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

_LOCAL_METHODS = ("subscribe", "unsubscribe", "disconnect", "methods")


class Dispatcher:
    """Routes JSON requests to the client/kernels, concurrently."""

    def __init__(self, client, stdin=None, stdout=None, pretty=False,
                 stream=False):
        self.client = client
        self.connections = {}
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout
        self.pretty = pretty
        self.stream = stream
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._workers = {}          # kernel_id -> (Queue, Thread)
        self._rest_workers = []
        self._rest_queue = queue.Queue()
        self._pending_input = {}    # request id -> Queue
        self._subscribed = {}       # kernel_id -> listener callable
        self._stopping = False

    # ----------------------------------------------------------------- output

    def _emit(self, obj):
        line = json.dumps(obj, indent=2 if self.pretty else None) + "\n"
        with self._write_lock:      # keep concurrent replies from interleaving
            self.stdout.write(line)
            self.stdout.flush()

    # ------------------------------------------------------------ connections

    def _connection(self, kernel_id):
        with self._state_lock:
            if kernel_id not in self.connections:
                self.connections[kernel_id] = self.client.kernel(kernel_id)
            return self.connections[kernel_id]

    def _drop_connection(self, kernel_id):
        with self._state_lock:
            conn = self.connections.pop(kernel_id, None)
            self._subscribed.pop(kernel_id, None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        return conn

    # ---------------------------------------------------------------- workers

    def _kernel_queue(self, kernel_id):
        """Per-kernel work queue: one socket, so one request at a time."""
        with self._state_lock:
            entry = self._workers.get(kernel_id)
            if entry is None:
                work = queue.Queue()
                thread = threading.Thread(
                    target=self._worker_loop, args=(work,),
                    name="jsonyter-kernel-" + kernel_id[:8], daemon=True)
                entry = (work, thread)
                self._workers[kernel_id] = entry
                thread.start()
            return entry[0]

    def _worker_loop(self, work):
        while True:
            request = work.get()
            if request is None:
                return
            self._run(request)

    def _rest_worker_loop(self):
        while True:
            request = self._rest_queue.get()
            if request is None:
                return
            self._run(request)

    def _start_rest_workers(self, count=4):
        for i in range(count):
            thread = threading.Thread(
                target=self._rest_worker_loop,
                name="jsonyter-rest-{}".format(i), daemon=True)
            thread.start()
            self._rest_workers.append(thread)

    def _run(self, request):
        try:
            self._emit(self.dispatch(request))
        except JupyterError as exc:
            self._emit({"id": request.get("id"), "error": exc.to_json()})
        except Exception as exc:    # keep the pipe alive on bugs
            self._emit({"id": request.get("id"), "error": {
                "error": type(exc).__name__, "message": str(exc)}})

    # ------------------------------------------------------------------ stdin

    def _stdin_callback(self, request_id):
        """Ask the front end for input; the reader thread routes the reply."""
        def ask(content):
            answer = queue.Queue()
            with self._state_lock:
                self._pending_input[request_id] = answer
            try:
                self._emit({"id": request_id, "input_request": content})
                return answer.get()
            finally:
                with self._state_lock:
                    self._pending_input.pop(request_id, None)
        return ask

    def _route_input(self, obj):
        value = obj.get("input", "")
        request_id = obj.get("id")
        with self._state_lock:
            if request_id is not None:
                answer = self._pending_input.get(request_id)
            elif len(self._pending_input) == 1:
                answer = next(iter(self._pending_input.values()))
            else:
                answer = None
        if answer is not None:
            answer.put(value)

    # -------------------------------------------------------------- dispatch

    def dispatch(self, request):
        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}

        if method in _CLIENT_METHODS:
            allowed = _CLIENT_METHODS[method]
            kwargs = {k: v for k, v in params.items() if k in allowed}
            args = []
            if "kernel_id" in kwargs:
                args = [kwargs.pop("kernel_id")]
            if "session_id" in kwargs:
                args = [kwargs.pop("session_id")]
            if method == "shutdown_kernel" and args:
                self._drop_connection(args[0])
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
                if params.get("stream", self.stream):
                    kwargs["on_output"] = lambda output: self._emit(
                        {"id": request_id, "output": output})
            result = getattr(conn, method)(**kwargs)
        elif method == "subscribe":
            result = self._subscribe(params.get("kernel_id"))
        elif method == "unsubscribe":
            result = self._unsubscribe(params.get("kernel_id"))
        elif method == "disconnect":
            conn = self._drop_connection(params.get("kernel_id"))
            result = {"id": params.get("kernel_id"), "closed": conn is not None}
        elif method == "methods":
            result = (sorted(_CLIENT_METHODS) + sorted(_KERNEL_METHODS)
                      + sorted(_LOCAL_METHODS))
        else:
            raise JupyterError("unknown method: {!r}".format(method))
        return {"id": request_id, "result": result}

    # ----------------------------------------------------------- subscriptions

    def _subscribe(self, kernel_id):
        if not kernel_id:
            raise JupyterError("missing required param: kernel_id")
        with self._state_lock:
            already = kernel_id in self._subscribed
        if already:
            return {"kernel_id": kernel_id, "subscribed": True}
        conn = self._connection(kernel_id)
        conn.connect()

        def listener(event):
            self._emit({"kernel_id": kernel_id, "event": event})

        conn.add_listener(listener)
        with self._state_lock:
            self._subscribed[kernel_id] = listener
        return {"kernel_id": kernel_id, "subscribed": True,
                "execution_state": conn.execution_state}

    def _unsubscribe(self, kernel_id):
        with self._state_lock:
            listener = self._subscribed.pop(kernel_id, None)
            conn = self.connections.get(kernel_id)
        if listener is not None and conn is not None:
            conn.remove_listener(listener)
        return {"kernel_id": kernel_id, "subscribed": False}

    # -------------------------------------------------------------- main loop

    def run(self):
        self._start_rest_workers()
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
            if not isinstance(request, dict):
                self._emit({"id": None, "error": {
                    "error": "ParseError",
                    "message": "request must be a JSON object"}})
                continue
            # An input reply, not a request.
            if "input" in request and "method" not in request:
                self._route_input(request)
                continue
            method = request.get("method")
            if method in _KERNEL_METHODS:
                kernel_id = request.get("params", {}).get("kernel_id")
                if kernel_id:
                    self._kernel_queue(kernel_id).put(request)
                    continue
            self._rest_queue.put(request)

    def shutdown(self):
        self._stopping = True
        with self._state_lock:
            workers = list(self._workers.values())
            connections = list(self.connections.values())
            self.connections.clear()
        for work, _thread in workers:
            work.put(None)
        for _ in self._rest_workers:
            self._rest_queue.put(None)
        for conn in connections:
            try:
                conn.close()
            except Exception:
                pass


def resolve_token(args):
    """Token from the least-exposed source available.

    ``--token`` is accepted for convenience but is visible to any local user
    via ``ps``; ``JUPYTER_TOKEN`` or ``--token-file`` (including ``-`` to read
    the first stdin line, which pairs with ``gpg -d | jsonyter``) keep it off
    the command line.
    """
    if args.token_file:
        if args.token_file == "-":
            return sys.stdin.readline().strip() or None
        with open(os.path.expanduser(args.token_file)) as handle:
            return handle.read().strip() or None
    if args.token:
        return args.token
    return os.environ.get("JUPYTER_TOKEN") or None


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="jsonyter",
        description="JSON-over-stdio bridge to a Jupyter server.")
    parser.add_argument("--url", default="http://localhost:8888",
                        help="Jupyter server base URL")
    parser.add_argument("--token", default=None,
                        help="Jupyter auth token (INSECURE: visible to other "
                             "local users via ps; prefer JUPYTER_TOKEN or "
                             "--token-file)")
    parser.add_argument("--token-file", default=None, metavar="PATH",
                        help="read the token from PATH, or from the first "
                             "line of stdin if PATH is '-' (e.g. "
                             "gpg -d token.gpg | jsonyter --token-file -)")
    parser.add_argument("--timeout", type=float, default=30.0,
                        help="timeout in seconds for REST calls and the "
                             "WebSocket handshake (not kernel execution)")
    parser.add_argument("--exec-timeout", type=float, default=None,
                        help="default timeout in seconds to wait for a "
                             "kernel reply on execute/complete/inspect/etc, "
                             "measured as silence since the last message "
                             "(not total run time); omit for no timeout "
                             "(wait indefinitely — the default, since some "
                             "kernels such as SAS are slow to respond)")
    parser.add_argument("--stream", action="store_true",
                        help="emit incremental {\"id\": N, \"output\": {...}} "
                             "lines for every execute, without each request "
                             "having to ask for \"stream\": true")
    parser.add_argument("--insecure", action="store_true",
                        help="skip TLS certificate verification")
    parser.add_argument("--pretty", action="store_true",
                        help="indent JSON responses (for humans; breaks the "
                             "one-line-per-response protocol editors rely on)")
    args = parser.parse_args(argv)

    client = Client(args.url, token=resolve_token(args) or False,
                    timeout=args.timeout, exec_timeout=args.exec_timeout,
                    verify_tls=not args.insecure)
    dispatcher = Dispatcher(client, pretty=args.pretty, stream=args.stream)
    try:
        dispatcher.run()
    except KeyboardInterrupt:
        pass
    finally:
        dispatcher.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
