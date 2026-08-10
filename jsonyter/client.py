"""HTTP client for the Jupyter Server REST API.

All methods return the server's JSON responses as plain Python objects
(dicts/lists), so every return value round-trips through ``json.dumps``.
Pass ``pretty=True`` to any public method to get an indented JSON string
instead (defaults to False).
"""

import functools
import json
import os

import requests


def prettifiable(method):
    """Give ``method`` a ``pretty`` keyword (default False).

    With ``pretty=True`` the method returns ``json.dumps(result, indent=2,
    sort_keys=True)`` instead of the plain Python object.
    """
    @functools.wraps(method)
    def wrapper(self, *args, pretty=False, **kwargs):
        result = method(self, *args, **kwargs)
        if pretty:
            return json.dumps(result, indent=2, sort_keys=True)
        return result
    return wrapper


class JupyterError(Exception):
    """Error talking to the Jupyter server, renderable as JSON."""

    def __init__(self, message, status=None, url=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.url = url

    def to_json(self):
        return {
            "error": type(self).__name__,
            "message": self.message,
            "status": self.status,
            "url": self.url,
        }


class Client:
    """Client for a local or remote Jupyter server.

    >>> client = Client("http://localhost:8888", token="...")
    >>> kernel = client.start_kernel("python3")
    >>> conn = client.kernel(kernel["id"])
    >>> conn.execute("1 + 1")
    """

    def __init__(self, base_url="http://localhost:8888", token=None,
                 timeout=10.0, exec_timeout=None, control_timeout=30.0,
                 verify_tls=True):
        """
        ``timeout`` bounds REST calls (``status``, ``start_kernel``, ...) and
        the initial WebSocket handshake — keep it short so a dead server
        fails fast.

        ``exec_timeout`` is the default wait for a kernel reply on
        ``execute``: how long to wait with *no message at all* from the
        kernel before giving up (each message received, including
        intermediate output, resets the clock — it is not a cap on total run
        time). Defaults to ``None``, meaning wait indefinitely, since a REPL
        shouldn't impose an arbitrary deadline on someone's code — some
        kernels (e.g. SAS) can also take a long time just to become
        responsive on a fresh connection. Use ``interrupt_kernel`` to reclaim
        a kernel that's actually stuck, or pass a finite ``exec_timeout``/
        per-call ``timeout`` if you want executions to give up on their own.

        ``control_timeout`` (default 30s) is the same kind of deadline for
        the introspection calls — ``complete``, ``inspect``, ``is_complete``,
        ``kernel_info`` and ``history``. Those are bounded,
        interactive-latency operations, so unlike ``execute`` they must not
        wait forever: kernels do exist that simply never answer some of them
        (the SAS kernel never replies to ``history_request``), and an
        unbounded wait there wedges the connection permanently. Pass ``None``
        to opt into waiting indefinitely anyway.

        ``token`` falls back to the ``JUPYTER_TOKEN`` environment variable
        when not given, so it never has to be hardcoded in a script. Pass
        ``token=False`` for an explicitly unauthenticated server.
        """
        if token is None:
            token = os.environ.get("JUPYTER_TOKEN") or None
        elif token is False:
            token = None
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.exec_timeout = exec_timeout
        self.control_timeout = control_timeout
        self._http = requests.Session()
        self._http.verify = verify_tls
        if token:
            self._http.headers["Authorization"] = "token " + token

    # ------------------------------------------------------------------ core

    def _request(self, method, path, json_body=None, params=None):
        url = self.base_url + path
        try:
            response = self._http.request(
                method, url, json=json_body, params=params,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise JupyterError(str(exc), url=url) from exc
        if response.status_code >= 400:
            try:
                detail = response.json().get("message", response.text)
            except ValueError:
                detail = response.text
            raise JupyterError(detail, status=response.status_code, url=url)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def _get(self, path, params=None):
        return self._request("GET", path, params=params)

    def _post(self, path, json_body=None):
        return self._request("POST", path, json_body=json_body)

    def _delete(self, path):
        return self._request("DELETE", path)

    # ---------------------------------------------------------------- server

    @prettifiable
    def status(self):
        """Server status: version, started, number of kernels, etc."""
        return self._get("/api/status")

    @prettifiable
    def version(self):
        return self._get("/api")

    # --------------------------------------------------------------- kernels

    @prettifiable
    def list_kernelspecs(self):
        """Available kernel types (name, display name, language, ...)."""
        return self._get("/api/kernelspecs")

    @prettifiable
    def list_kernels(self):
        return self._get("/api/kernels")

    @prettifiable
    def start_kernel(self, name=None):
        """Start a kernel; ``name`` defaults to the server's default spec."""
        body = {"name": name} if name else {}
        return self._post("/api/kernels", body)

    @prettifiable
    def get_kernel(self, kernel_id):
        return self._get("/api/kernels/" + kernel_id)

    @prettifiable
    def shutdown_kernel(self, kernel_id):
        self._delete("/api/kernels/" + kernel_id)
        return {"id": kernel_id, "shutdown": True}

    @prettifiable
    def restart_kernel(self, kernel_id):
        return self._post("/api/kernels/" + kernel_id + "/restart")

    @prettifiable
    def interrupt_kernel(self, kernel_id):
        self._post("/api/kernels/" + kernel_id + "/interrupt")
        return {"id": kernel_id, "interrupted": True}

    # -------------------------------------------------------------- sessions

    @prettifiable
    def list_sessions(self):
        return self._get("/api/sessions")

    @prettifiable
    def create_session(self, path, kernel_name=None, session_type="console",
                       name=""):
        """Create a named session bound to a (possibly new) kernel.

        Sessions let a REPL reconnect to the same kernel later by path.
        """
        return self._post("/api/sessions", {
            "path": path,
            "type": session_type,
            "name": name,
            "kernel": {"name": kernel_name} if kernel_name else {},
        })

    @prettifiable
    def get_session(self, session_id):
        return self._get("/api/sessions/" + session_id)

    @prettifiable
    def delete_session(self, session_id):
        self._delete("/api/sessions/" + session_id)
        return {"id": session_id, "deleted": True}

    # -------------------------------------------------------------- contents

    @prettifiable
    def get_contents(self, path="", content=True):
        """File/notebook contents at ``path`` (notebooks come back as JSON)."""
        params = {"content": "1" if content else "0"}
        return self._get("/api/contents/" + path.lstrip("/"), params=params)

    # ------------------------------------------------------- local notebooks
    # Filesystem operations: no server contact, so they work offline.

    @prettifiable
    def read_notebook(self, path):
        """Local ``.ipynb`` as normalized v4 JSON, every cell carrying an id."""
        from .notebook import read_notebook
        return read_notebook(path)

    @prettifiable
    def write_notebook(self, path, cells, expect_hash=None):
        """Merge cell ``source`` into a local ``.ipynb``, preserving outputs."""
        from .notebook import write_notebook
        return write_notebook(path, cells, expect_hash=expect_hash)

    @prettifiable
    def notebook_hash(self, path):
        """sha256 of a local notebook, for the ``expect_hash`` staleness guard."""
        from .notebook import file_hash
        return {"path": path, "hash": file_hash(path)}

    # --------------------------------------------------------------- kernels'
    # websocket connections

    def kernel(self, kernel_id):
        """A :class:`~jsonyter.kernel.KernelConnection` for ``kernel_id``."""
        from .kernel import KernelConnection
        return KernelConnection(self, kernel_id)
