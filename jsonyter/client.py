"""HTTP client for the Jupyter Server REST API.

All methods return the server's JSON responses as plain Python objects
(dicts/lists), so every return value round-trips through ``json.dumps``.
"""

import requests


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
                 timeout=10.0, verify_tls=True):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
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

    def status(self):
        """Server status: version, started, number of kernels, etc."""
        return self._get("/api/status")

    def version(self):
        return self._get("/api")

    # --------------------------------------------------------------- kernels

    def list_kernelspecs(self):
        """Available kernel types (name, display name, language, ...)."""
        return self._get("/api/kernelspecs")

    def list_kernels(self):
        return self._get("/api/kernels")

    def start_kernel(self, name=None):
        """Start a kernel; ``name`` defaults to the server's default spec."""
        body = {"name": name} if name else {}
        return self._post("/api/kernels", body)

    def get_kernel(self, kernel_id):
        return self._get("/api/kernels/" + kernel_id)

    def shutdown_kernel(self, kernel_id):
        self._delete("/api/kernels/" + kernel_id)
        return {"id": kernel_id, "shutdown": True}

    def restart_kernel(self, kernel_id):
        return self._post("/api/kernels/" + kernel_id + "/restart")

    def interrupt_kernel(self, kernel_id):
        self._post("/api/kernels/" + kernel_id + "/interrupt")
        return {"id": kernel_id, "interrupted": True}

    # -------------------------------------------------------------- sessions

    def list_sessions(self):
        return self._get("/api/sessions")

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

    def get_session(self, session_id):
        return self._get("/api/sessions/" + session_id)

    def delete_session(self, session_id):
        self._delete("/api/sessions/" + session_id)
        return {"id": session_id, "deleted": True}

    # -------------------------------------------------------------- contents

    def get_contents(self, path="", content=True):
        """File/notebook contents at ``path`` (notebooks come back as JSON)."""
        params = {"content": "1" if content else "0"}
        return self._get("/api/contents/" + path.lstrip("/"), params=params)

    # --------------------------------------------------------------- kernels'
    # websocket connections

    def kernel(self, kernel_id):
        """A :class:`~jsonyter.kernel.KernelConnection` for ``kernel_id``."""
        from .kernel import KernelConnection
        return KernelConnection(self, kernel_id)
