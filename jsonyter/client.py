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

# Statuses a gateway like Cloudflare emits on its own behalf. A response with
# one of these *plus* a ``cf-ray`` header and ``server: cloudflare`` is the
# proxy talking, not the Jupyter server — a different set of knobs fixes it,
# so the error message has to say which layer refused.
_PROXY_STATUS = frozenset({413, 429, 502, 503, 504, 520, 521, 522, 523, 524,
                           525, 526, 530})


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

    def __init__(self, message, status=None, url=None, cf_ray=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.url = url
        # Set only when the failing response carried a ``cf-ray`` header, so a
        # front end can tell "the proxy refused" from "the server refused".
        self.cf_ray = cf_ray

    def to_json(self):
        payload = {
            "error": type(self).__name__,
            "message": self.message,
            "status": self.status,
            "url": self.url,
        }
        if self.cf_ray:
            payload["cf_ray"] = self.cf_ray
        return payload


class Client:
    """Client for a local or remote Jupyter server.

    >>> client = Client("http://localhost:8888", token="...")
    >>> kernel = client.start_kernel("python3")
    >>> conn = client.kernel(kernel["id"])
    >>> conn.execute("1 + 1")
    """

    def __init__(self, base_url="http://localhost:8888", token=None,
                 timeout=10.0, exec_timeout=None, control_timeout=30.0,
                 verify_tls=True, export_timeout=120.0, transfer_timeout=300.0):
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

        ``export_timeout`` (default 120s) bounds a single ``export_notebook``
        request. A real notebook's PDF render is seconds-to-minutes, while
        ``timeout`` exists to make a dead server fail fast — so export gets
        its own, much larger, deadline.

        ``transfer_timeout`` (default 300s) is the per-request deadline for
        the chunked file-transfer calls (``put_contents``/``get_contents``
        while an ``upload``/``download`` is running, and the ranged
        ``/files/`` reads). One 8 MiB chunk over a slow uplink, or hashing a
        multi-GB file server-side, routinely outlasts ``timeout``; a whole
        transfer is still unbounded, only each request is capped.

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
        self.export_timeout = export_timeout
        self.transfer_timeout = transfer_timeout
        self._http = requests.Session()
        self._http.verify = verify_tls
        if token:
            self._http.headers["Authorization"] = "token " + token

    # ------------------------------------------------------------------ core

    def _request(self, method, path, json_body=None, params=None, timeout=None):
        url = self.base_url + path
        try:
            response = self._http.request(
                method, url, json=json_body, params=params,
                timeout=self.timeout if timeout is None else timeout,
            )
        except requests.RequestException as exc:
            raise JupyterError(str(exc), url=url) from exc
        if response.status_code >= 400:
            cf_ray = response.headers.get("cf-ray")
            try:
                detail = response.json().get("message", response.text)
            except ValueError:
                detail = response.text
            if (cf_ray and response.status_code in _PROXY_STATUS
                    and "cloudflare" in response.headers.get(
                        "server", "").lower()):
                detail = ("{} — HTTP {} from the proxy (cf-ray {} present), "
                          "not from the Jupyter server".format(
                              (detail or "").strip() or response.reason,
                              response.status_code, cf_ray))
            raise JupyterError(detail, status=response.status_code, url=url,
                               cf_ray=cf_ray)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def _request_raw(self, method, path, json_body=None, params=None, timeout=None):
        """Like _request, but returns the raw requests.Response.

        Redirects are NOT followed: /nbconvert is outside /api, so an
        unauthenticated GET answers 302 -> /login -> 200 HTML, which would
        otherwise be handed back as a successful export. Status codes are
        not treated as errors here — the caller interprets them, since
        status semantics differ from the JSON API.
        """
        url = self.base_url + path
        try:
            response = self._http.request(
                method, url, json=json_body, params=params,
                timeout=timeout if timeout is not None else self.timeout,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise JupyterError(str(exc), url=url) from exc
        return response

    def _get(self, path, params=None, timeout=None):
        return self._request("GET", path, params=params, timeout=timeout)

    def _post(self, path, json_body=None, timeout=None):
        return self._request("POST", path, json_body=json_body, timeout=timeout)

    def _put(self, path, json_body=None, timeout=None):
        return self._request("PUT", path, json_body=json_body, timeout=timeout)

    def _patch(self, path, json_body=None, timeout=None):
        return self._request("PATCH", path, json_body=json_body, timeout=timeout)

    def _delete(self, path, timeout=None):
        return self._request("DELETE", path, timeout=timeout)

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
    # Thin wrappers over the Jupyter Contents API. The chunked file-transfer
    # orchestration (upload/download/kernel_contents_dir) lives in
    # ``jsonyter.transfer`` and drives these.

    @prettifiable
    def get_contents(self, path="", content=True, type=None, format=None,
                     hash=False, timeout=None):
        """``GET /api/contents/<path>`` — a file / notebook / directory model.

        ``content=False`` returns metadata only (``size``, ``last_modified``,
        ``type``, ...), which keeps a whole-file read off the wire.
        ``format="base64"`` forces byte-exact retrieval of a file that would
        otherwise be decoded as text. ``hash=True`` adds ``hash`` and
        ``hash_algorithm`` (sha256) to the model on jupyter_server >= 2.11
        and composes with ``content=False`` — the server re-reads the bytes
        to hash them, so the digest itself costs no download. Servers older
        than 2.11 ignore the argument and return no ``hash`` key.
        """
        params = {"content": "1" if content else "0"}
        if type is not None:
            params["type"] = type
        if format is not None:
            params["format"] = format
        if hash:
            params["hash"] = "1"
        return self._get("/api/contents/" + path.lstrip("/"), params=params,
                         timeout=timeout)

    @prettifiable
    def put_contents(self, path, content, type="file", format="base64",
                     chunk=None, timeout=None):
        """``PUT /api/contents/<path>`` — create or overwrite.

        ``chunk`` drives the large-file protocol of ``AsyncLargeFileManager``
        (the default contents manager): ``1`` creates / truncates, ``2..n``
        append, and ``-1`` appends the final piece and runs post-save hooks.
        Only ``type="file"`` is chunkable server-side. A lone ``chunk=1`` with
        no ``-1`` to follow never runs the hooks, so a file that fits in one
        chunk should be sent with ``chunk=None`` (a plain save).
        """
        model = {"type": type, "format": format, "content": content}
        if chunk is not None:
            model["chunk"] = chunk
        return self._put("/api/contents/" + path.lstrip("/"), model,
                         timeout=timeout)

    @prettifiable
    def make_directory(self, path, timeout=None):
        """``PUT /api/contents/<path>`` with ``{"type": "directory"}``.

        Names the directory exactly, unlike ``POST`` (which creates an
        "Untitled Folder").
        """
        return self._put("/api/contents/" + path.lstrip("/"),
                         {"type": "directory"}, timeout=timeout)

    @prettifiable
    def delete_contents(self, path, timeout=None):
        """``DELETE /api/contents/<path>``."""
        self._delete("/api/contents/" + path.lstrip("/"), timeout=timeout)
        return {"path": path.strip("/"), "deleted": True}

    @prettifiable
    def rename_contents(self, path, new_path, timeout=None):
        """``PATCH /api/contents/<path>`` with ``{"path": new_path}``.

        The URL carries the *old* path, the body the *new* one. Moves and
        renames are the same operation.
        """
        return self._patch("/api/contents/" + path.lstrip("/"),
                           {"path": new_path.lstrip("/")}, timeout=timeout)

    @prettifiable
    def copy_contents(self, path, to_dir, timeout=None):
        """``POST /api/contents/<to_dir>`` with ``{"copy_from": path}``.

        A server-side copy — no bytes cross the wire. The server picks the
        destination *name* (appending "-Copy1" and so on); read it back from
        the returned model rather than assuming it.
        """
        return self._post("/api/contents/" + to_dir.lstrip("/"),
                          {"copy_from": path.lstrip("/")}, timeout=timeout)

    @prettifiable
    def list_contents(self, path="", timeout=None):
        """Directory listing — ``get_contents`` on a directory.

        Children arrive under ``content`` without their own ``content`` but
        with ``name`` / ``path`` / ``type`` / ``size`` / ``last_modified`` /
        ``writable``.
        """
        return self._get("/api/contents/" + path.lstrip("/"),
                         params={"content": "1"}, timeout=timeout)

    # ------------------------------------------------------- local notebooks
    # Filesystem operations: no server contact, so they work offline.

    @prettifiable
    def read_notebook(self, path):
        """Local ``.ipynb`` as normalized v4 JSON, every cell carrying an id."""
        from .notebook import read_notebook
        return read_notebook(path)

    @prettifiable
    def write_notebook(self, path, cells, expect_hash=None,
                       include_outputs=False):
        """Merge cell ``source`` into a local ``.ipynb``, preserving outputs.

        ``include_outputs=True`` also persists per-cell ``outputs``/
        ``execution_count`` for this save; by default they are ignored.
        """
        from .notebook import write_notebook
        return write_notebook(path, cells, expect_hash=expect_hash,
                              include_outputs=include_outputs)

    @prettifiable
    def notebook_hash(self, path):
        """sha256 of a local notebook, for the ``expect_hash`` staleness guard."""
        from .notebook import file_hash
        return {"path": path, "hash": file_hash(path)}

    # ----------------------------------------------------------------- export

    @prettifiable
    def list_export_formats(self):
        """Export formats this server offers, or why it offers none."""
        from .export import list_export_formats
        return list_export_formats(self)

    @prettifiable
    def export_notebook(self, format=None, *, server_path=None, cells=None,
                        notebook=None, name=None, to_path=None,
                        include_outputs=None, sanitize_html=None, timeout=None):
        """Export a notebook through the server's nbconvert endpoint."""
        from .export import export_notebook
        return export_notebook(
            self, format, server_path=server_path, cells=cells,
            notebook=notebook, name=name, to_path=to_path,
            include_outputs=include_outputs, sanitize_html=sanitize_html,
            timeout=timeout)

    # --------------------------------------------------------------- kernels'
    # websocket connections

    def kernel(self, kernel_id):
        """A :class:`~jsonyter.kernel.KernelConnection` for ``kernel_id``."""
        from .kernel import KernelConnection
        return KernelConnection(self, kernel_id)
