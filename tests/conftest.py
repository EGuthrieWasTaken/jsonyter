"""Fakes for the file-transfer tests.

The protocol-shape tests run against an in-memory Contents API rather than a
live ``jupyter server``: a :class:`FakeContentsServer` holds the files, and a
:class:`FakeSession` stands in for the ``requests.Session`` a
:class:`jsonyter.Client` would otherwise own. One opt-in integration test
(``test_integration.py``, skipped unless ``JSONYTER_LIVE_URL`` is set) covers
the real thing.
"""

import base64
import hashlib
import json
import posixpath
import re
import urllib.parse

import pytest
import requests.structures

import jsonyter


TS = "2026-09-07T14:22:00.000000Z"


# --------------------------------------------------------------- fake HTTP

class FakeResponse:
    def __init__(self, status_code, *, json_body=None, body=b"", headers=None,
                 reason="", url=""):
        self.status_code = status_code
        self._json = json_body
        if isinstance(body, str):
            body = body.encode("utf-8")
        # Real ``requests`` always fills ``.content`` with the raw body, even
        # for JSON; ``Client._request`` keys off ``not response.content``.
        if not body and json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
        self.content = body
        self.reason = reason
        self.url = url
        self.headers = requests.structures.CaseInsensitiveDict(headers or {})

    @property
    def text(self):
        return self.content.decode("utf-8", "replace")

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def iter_content(self, chunk_size=8192):
        for i in range(0, len(self.content), max(1, chunk_size)):
            yield self.content[i:i + chunk_size]

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)

    def close(self):
        pass


class FakeSession:
    """Just enough of ``requests.Session`` for ``Client`` and ``transfer``."""

    def __init__(self, server):
        self.server = server
        self.headers = {}
        self.verify = True
        self.requests = []          # (method, path, params, json, headers)

    def request(self, method, url, json=None, params=None, timeout=None,
                allow_redirects=True, headers=None, stream=False):
        split = urllib.parse.urlsplit(url)
        path = urllib.parse.unquote(split.path)
        query = dict(urllib.parse.parse_qsl(split.query))
        query.update(params or {})
        self.requests.append((method, path, query, json, dict(headers or {})))
        return self.server.handle(method, path, query, json, headers or {}, url)

    def get(self, url, headers=None, params=None, stream=False, timeout=None,
            allow_redirects=True):
        return self.request("GET", url, params=params, headers=headers,
                            stream=stream, allow_redirects=allow_redirects)


# ------------------------------------------------------- fake contents API

class FakeContentsServer:
    def __init__(self):
        self.files = {}             # "a/b.csv" -> bytearray
        self.dirs = {""}            # contents paths known to be directories
        self.no_hash = False        # simulate jupyter_server < 2.11
        self.corrupt_hash = False   # return a wrong digest
        self.ignore_range = False   # /files/ answers 200 to any Range
        self.no_files = False       # /files/ 404s (non-file-backed manager)
        self.force = None           # (status, json_body, headers) for any call
        self.fail_put_after = None  # (n, status, headers): PUT n+1 fails
        self._put_count = 0
        self.range_200_after = None  # honour the probe, then answer 200 to ranges
        self.range_error_after = None  # (n, status, headers): range n+1 errors
        self._range_count = 0

    # -- helpers ------------------------------------------------------------

    def _digest(self, data):
        if self.corrupt_hash:
            return "0" * 64
        return hashlib.sha256(bytes(data)).hexdigest()

    def _file_model(self, cpath, want_content, want_hash, fmt):
        data = self.files[cpath]
        model = {"type": "file", "name": posixpath.basename(cpath),
                 "path": cpath, "size": len(data), "last_modified": TS,
                 "writable": True, "format": None, "content": None}
        if want_hash and not self.no_hash:
            model["hash"] = self._digest(data)
            model["hash_algorithm"] = "sha256"
        if want_content:
            if fmt == "base64":
                model["format"] = "base64"
                model["content"] = base64.b64encode(bytes(data)).decode("ascii")
            else:
                model["format"] = "text"
                model["content"] = bytes(data).decode("utf-8", "replace")
        return model

    def _children(self, cpath):
        prefix = cpath + "/" if cpath else ""
        out = []
        for f, data in self.files.items():
            if f.startswith(prefix) and "/" not in f[len(prefix):]:
                out.append({"type": "file", "name": posixpath.basename(f),
                            "path": f, "size": len(data),
                            "last_modified": TS, "writable": True})
        for d in self.dirs:
            if d and d.startswith(prefix) and "/" not in d[len(prefix):]:
                out.append({"type": "directory", "name": posixpath.basename(d),
                            "path": d, "size": None, "last_modified": TS,
                            "writable": True})
        return out

    def _dir_model(self, cpath, want_content):
        return {"type": "directory", "name": posixpath.basename(cpath),
                "path": cpath, "size": None, "last_modified": TS,
                "writable": True, "format": "json",
                "content": self._children(cpath) if want_content else None}

    # -- routing ----------------------------------------------------------

    def handle(self, method, path, query, body, headers, url):
        if self.force is not None:
            status, jb, hh = self.force
            return FakeResponse(status, json_body=jb, headers=hh, url=url,
                                reason="Forced")
        if path.startswith("/files/"):
            return self._files(path[len("/files/"):], headers, url)
        if path == "/api/contents" or path.startswith("/api/contents/"):
            cpath = path[len("/api/contents"):].strip("/")
            return self._contents(method, cpath, query, body, url)
        if path.startswith("/api/kernels/"):
            # Enough for restart/shutdown to succeed in the wiring tests.
            return FakeResponse(200, json_body={
                "id": path.split("/")[3], "name": "python3",
                "execution_state": "starting"}, url=url)
        return FakeResponse(404, json_body={"message": "not found"}, url=url)

    def _err(self, status, msg, url):
        return FakeResponse(status, json_body={"message": msg}, url=url,
                            reason=msg)

    def _contents(self, method, cpath, query, body, url):
        if method == "GET":
            want_content = query.get("content", "1") == "1"
            want_hash = query.get("hash") == "1"
            fmt = query.get("format")
            if cpath in self.dirs:
                return FakeResponse(200,
                                    json_body=self._dir_model(cpath, want_content),
                                    url=url)
            if cpath in self.files:
                return FakeResponse(
                    200,
                    json_body=self._file_model(cpath, want_content, want_hash,
                                               fmt),
                    url=url)
            return self._err(404, "No such file or directory: " + cpath, url)

        if method == "PUT":
            body = body or {}
            if body.get("type") == "directory":
                self.dirs.add(cpath)
                return FakeResponse(201,
                                    json_body=self._dir_model(cpath, False),
                                    url=url)
            if self.fail_put_after is not None:
                n, status, hh = self.fail_put_after
                self._put_count += 1
                if self._put_count > n:
                    return FakeResponse(status, body=b"<html>cloudflare</html>",
                                        json_body={"message": "Payload Too Large"},
                                        headers=hh, url=url, reason="Too Large")
            raw = self._decode(body)
            chunk = body.get("chunk")
            if chunk in (None, 1):
                self.files[cpath] = bytearray(raw)
            else:
                self.files.setdefault(cpath, bytearray()).extend(raw)
            return FakeResponse(201,
                                json_body=self._file_model(cpath, False, False,
                                                           None),
                                url=url)

        if method == "PATCH":
            body = body or {}
            new = (body.get("path") or "").strip("/")
            if cpath not in self.files:
                return self._err(404, "No such file: " + cpath, url)
            self.files[new] = self.files.pop(cpath)
            return FakeResponse(200,
                                json_body=self._file_model(new, False, False,
                                                           None),
                                url=url)

        if method == "POST":
            body = body or {}
            src = (body.get("copy_from") or "").strip("/")
            if src not in self.files:
                return self._err(404, "No such file: " + src, url)
            stem, dot, ext = posixpath.basename(src).partition(".")
            name = "{}-Copy1{}{}".format(stem, dot, ext)
            dest = (cpath + "/" + name) if cpath else name
            self.files[dest] = bytearray(self.files[src])
            return FakeResponse(201,
                                json_body=self._file_model(dest, False, False,
                                                           None),
                                url=url)

        if method == "DELETE":
            self.files.pop(cpath, None)
            self.dirs.discard(cpath)
            return FakeResponse(204, url=url)

        return self._err(405, "method not allowed", url)

    def _decode(self, body):
        content = body.get("content") or ""
        if body.get("format") == "base64":
            return base64.b64decode(content)
        return content.encode("utf-8")

    def _files(self, quoted, headers, url):
        fpath = quoted
        if self.no_files or fpath not in self.files:
            return FakeResponse(404, body=b"not found", url=url)
        data = bytes(self.files[fpath])
        rng = None
        for k, v in headers.items():
            if k.lower() == "range":
                rng = v
        if self.ignore_range or not rng:
            return FakeResponse(200, body=data, url=url,
                                headers={"Accept-Ranges":
                                         "none" if self.ignore_range else "bytes"})
        m = re.match(r"bytes=(\d+)-(\d*)", rng)
        start = int(m.group(1))
        is_probe = start == 0 and m.group(2) == "0"
        if not is_probe and (self.range_200_after is not None
                             or self.range_error_after is not None):
            self._range_count += 1
            if (self.range_200_after is not None
                    and self._range_count > self.range_200_after):
                # Honoured the bytes=0-0 probe, then ignores real ranges.
                return FakeResponse(200, body=data, url=url)
            if (self.range_error_after is not None
                    and self._range_count > self.range_error_after[0]):
                _n, status, hh = self.range_error_after
                return FakeResponse(status, body=b"<html>cloudflare</html>",
                                    headers=hh, url=url, reason="Unavailable")
        if not data or start >= len(data):
            return FakeResponse(200, body=data, url=url)
        end = int(m.group(2)) if m.group(2) else len(data) - 1
        end = min(end, len(data) - 1)
        return FakeResponse(
            206, body=data[start:end + 1], url=url,
            headers={"Content-Range": "bytes {}-{}/{}".format(start, end,
                                                              len(data)),
                     "Accept-Ranges": "bytes"})


# ------------------------------------------------------------ fake kernel

class FakeConn:
    """A stand-in kernel connection for ``kernel_contents_dir``."""

    _PROBE_RE = re.compile(r"\.jsonyter-probe-[0-9a-f]+")

    def __init__(self, server, *, language="python", cwd="/srv/work/data",
                 contents_prefix="work/data", visible=True, fail_cwd=False,
                 fail_touch=False):
        self.server = server
        self.language = language
        self.cwd = cwd
        self.contents_prefix = contents_prefix
        self.visible = visible
        self.fail_cwd = fail_cwd
        self.fail_touch = fail_touch
        self.executed = []

    def kernel_info(self):
        return {"language_info": {"name": self.language}}

    def _sentinel_path(self, name):
        if not self.contents_prefix:
            return name
        return self.contents_prefix + "/" + name

    def execute(self, code, silent=False, store_history=True, timeout=None):
        self.executed.append(code)
        if any(tok in code for tok in ("getcwd", "pwd()", "getwd()")):
            if self.fail_cwd:
                return {"outputs": [{"type": "error", "ename": "OSError",
                                     "evalue": "boom"}]}
            return {"outputs": [{"type": "stream", "name": "stdout",
                                 "text": self.cwd}]}
        m = self._PROBE_RE.search(code)
        is_remove = bool(m) and ("remove" in code or re.search(r"\brm\(", code))
        if m and not is_remove and self.fail_touch:
            return {"outputs": [{"type": "error", "ename": "PermissionError",
                                 "evalue": "read-only cwd"}]}
        if m and self.visible:
            path = self._sentinel_path(m.group(0))
            if is_remove:
                self.server.files.pop(path, None)
            else:
                if self.contents_prefix:
                    self.server.dirs.add(self.contents_prefix)
                self.server.files[path] = bytearray()
        return {"outputs": []}

    def close(self):
        pass


# --------------------------------------------------------------- fixtures

def build_client(server=None, **client_kw):
    server = server or FakeContentsServer()
    client = jsonyter.Client("http://jupyter.test", token="tok", **client_kw)
    client._http = FakeSession(server)
    return client, server


@pytest.fixture
def client_server():
    return build_client()


@pytest.fixture
def tmp_file(tmp_path):
    def _make(data, name="src.bin"):
        p = tmp_path / name
        p.write_bytes(data if isinstance(data, bytes) else data.encode())
        return str(p)
    return _make
