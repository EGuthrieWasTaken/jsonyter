"""Notebook export via the Jupyter server's nbconvert endpoints.

This is a wrapper, not a conversion job — nbconvert always runs server-side.
``GET /nbconvert/{format}/{path}`` exports a notebook already saved in the
server's contents namespace; ``POST /nbconvert/{format}`` exports one sent in
the request body, which is the only way to export in-memory buffer state (an
unsaved REPL front end, or freshly generated cell outputs) without touching
the file on disk.

Both endpoints return raw documents, not JSON, and some formats (markdown
with images, in particular) come back as a zip of a document plus sidecar
files rather than a single document — this module always unpacks that zip so
callers never see one. Format strings are passed through to the server
unmodified: ``BINARY_FORMATS``/``EXTENSIONS``/``TOOLCHAIN_HINTS`` below are
hints with fallbacks, not an allow-list, so third-party exporters registered
by entry point work unchanged.
"""

import base64
import copy
import hashlib
import html
import io
import mimetypes
import os
import re
import shutil
import tempfile
import urllib.parse
import zipfile

from .client import JupyterError
from .notebook import _merge_cells, _nbformat, _validate_specs

# Formats whose bytes are binary regardless of what the server claims.
# webpdf/qtpdf/qtpng inherit HTMLExporter's "text/html" mimetype but emit
# PDF/PNG bytes, so the advertised mimetype cannot be trusted for these.
BINARY_FORMATS = frozenset({"pdf", "webpdf", "qtpdf", "qtpng"})

EXTENSIONS = {
    "asciidoc": ".asciidoc", "custom": ".txt",  "html":     ".html",
    "latex":    ".tex",      "markdown": ".md",  "notebook": ".ipynb",
    "pdf":      ".pdf",      "python":   ".py",  "qtpdf":    ".pdf",
    "qtpng":    ".png",      "rst":      ".rst", "script":   ".txt",
    "slides":   ".slides.html", "webpdf": ".pdf",
}

# Appended to the server's own error message as an actionable hint.
TOOLCHAIN_HINTS = {
    "pdf":    "the 'pdf' exporter needs pandoc and a LaTeX engine (xelatex) "
              "installed on the Jupyter server",
    "latex":  "the 'latex' exporter needs pandoc installed on the Jupyter server",
    "webpdf": "the 'webpdf' exporter needs playwright and a chromium build on "
              "the Jupyter server (pip install 'nbconvert[webpdf]' && "
              "playwright install chromium)",
    "qtpdf":  "the 'qtpdf' exporter needs pyqtwebengine on the Jupyter server",
    "qtpng":  "the 'qtpng' exporter needs pyqtwebengine on the Jupyter server",
}

_MAX_ERROR_DETAIL = 2000
_TRACEBACK_RE = re.compile(r'<pre class="traceback">(.*?)</pre>', re.S)
_H1_RE = re.compile(r'<h1>(.*?)</h1>')


class ExportError(JupyterError):
    """An export could not be produced, with the server's own reason."""

    def __init__(self, message, status=None, url=None, format=None,
                hint=None, available_formats=None):
        super().__init__(message, status=status, url=url)
        self.format = format
        self.hint = hint
        self.available_formats = available_formats

    def to_json(self):
        payload = super().to_json()
        payload.update({"format": self.format, "hint": self.hint,
                        "available_formats": self.available_formats})
        return payload


# -------------------------------------------------------------- error pages

def _unescape_match(match):
    return html.unescape(match.group(1)).strip()[:_MAX_ERROR_DETAIL]


def _error_detail(response):
    """Recover the server's real error message from an HTML error page.

    Conversion failures are HTTP 500 with an HTML page whose body carries the
    actual message in ``<pre class="traceback">``; some failures (unknown
    format, missing notebook) have no traceback and only a bare ``<h1>``.
    """
    match = _TRACEBACK_RE.search(response.text)
    if match:
        detail = _unescape_match(match)
        prefix = "nbconvert failed: "
        if detail.startswith(prefix):
            detail = detail[len(prefix):]
        return detail
    match = _H1_RE.search(response.text)
    if match:
        return _unescape_match(match)
    return "HTTP {} {}".format(response.status_code, response.reason)


# ---------------------------------------------------------- format discovery

def list_export_formats(client):
    """Export formats this server offers, or why it offers none.

    Never raises for an unavailable endpoint — it is a capability probe, not
    an assertion that export works.
    """
    response = client._request_raw("GET", "/api/nbconvert")
    status = response.status_code
    if status == 200:
        return {"available": True, "formats": response.json(), "reason": None}
    if 300 <= status < 400:
        return {"available": False, "formats": {},
                "reason": "authentication failed: the server redirected to "
                          "its login page; check the token"}
    if status == 404:
        return {"available": False, "formats": {},
                "reason": "this server does not serve the nbconvert endpoints"}
    if status == 500:
        return {"available": False, "formats": {}, "reason": _error_detail(response)}
    raise JupyterError(_error_detail(response), status=status, url=response.url)


# ------------------------------------------------------------------ helpers

def _document_stem(server_path, name):
    base = os.path.basename(server_path) if server_path is not None else (
        name or "notebook.ipynb")
    if base.endswith(".ipynb"):
        base = base[:-len(".ipynb")]
    return base or "notebook"


def _normalize_raw_notebook(notebook, include_outputs):
    """Make an on-disk-shaped ``.ipynb`` dict acceptable to POST /nbconvert.

    Raw JSON stores ``source`` as a list of lines; the server needs a plain
    string. Never mutates the caller's dict.
    """
    if not isinstance(notebook, dict):
        raise JupyterError("notebook must be an object (nbformat JSON)")
    nb = copy.deepcopy(notebook)
    for cell in nb.get("cells", []):
        source = cell.get("source")
        if isinstance(source, list):
            cell["source"] = "".join(source)
        if not include_outputs and cell.get("cell_type") == "code":
            cell.pop("outputs", None)
            cell.pop("execution_count", None)
    return nb


def _encode_bytes(data):
    try:
        return "text", data.decode("utf-8")
    except UnicodeDecodeError:
        return "base64", base64.b64encode(data).decode("ascii")


def _encode_body(data, format):
    if format in BINARY_FORMATS:
        return "base64", base64.b64encode(data).decode("ascii")
    return _encode_bytes(data)


def _unpack_bundle(body, format, document_stem):
    """Unpack a zip response into (primary_name, primary_bytes, resources).

    ``resources`` is a list of ``(basename, bytes)`` for every other member.
    Member names are sanitized to their basename so a crafted zip cannot
    write outside the target directory.
    """
    archive = zipfile.ZipFile(io.BytesIO(body))
    members = []
    for info in archive.infolist():
        if info.is_dir():
            continue
        safe_name = os.path.basename(info.filename)
        if safe_name in ("", ".", ".."):
            continue
        members.append((safe_name, archive.read(info)))

    if not members:
        raise ExportError(
            "the server returned an empty document for format {!r}".format(format),
            format=format)

    primary_index = None
    for index, (name, _data) in enumerate(members):
        if os.path.splitext(name)[0] == document_stem:
            primary_index = index
            break
    if primary_index is None:
        wanted_ext = EXTENSIONS.get(format)
        if wanted_ext:
            for index, (name, _data) in enumerate(members):
                if name.endswith(wanted_ext):
                    primary_index = index
                    break
    if primary_index is None:
        primary_index = 0

    primary_name, primary_bytes = members[primary_index]
    resources = [item for index, item in enumerate(members)
                 if index != primary_index]
    return primary_name, primary_bytes, resources


def _resource_mimetype(name):
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def _inline_result(format, mimetype, extension, primary_bytes, resource_items,
                   bundle):
    encoding, content = _encode_body(primary_bytes, format)
    resources = []
    for name, data in resource_items:
        res_encoding, res_content = _encode_bytes(data)
        resources.append({"name": name, "mimetype": _resource_mimetype(name),
                          "encoding": res_encoding, "content": res_content})
    return {"format": format, "mimetype": mimetype, "extension": extension,
            "encoding": encoding, "content": content, "bundle": bundle,
            "resources": resources}


def _atomic_write_bytes(data, path):
    """Serialize ``data`` to a temp file in ``path``'s directory, then move it
    into place. Mirrors ``notebook._atomic_write``, adapted for raw bytes."""
    directory = os.path.dirname(path) or "."
    handle = None
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".jsonyter-")
        handle = os.fdopen(fd, "wb")
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        handle = None
        if os.path.exists(path):
            shutil.copymode(path, tmp_path)      # keep the original's mode
        os.replace(tmp_path, path)               # atomic; never truncates
        tmp_path = None
    except JupyterError:
        raise
    except Exception as exc:
        raise JupyterError(
            "could not write export {}: {}".format(path, exc)) from exc
    finally:
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass
        if tmp_path is not None and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _write_to_path(format, mimetype, extension, primary_bytes, resource_items,
                   document_stem, to_path, bundle):
    is_dir_target = (os.path.isdir(to_path) or to_path.endswith(os.sep)
                     or (os.altsep and to_path.endswith(os.altsep)))
    if is_dir_target:
        primary_path = os.path.join(to_path, document_stem + extension)
    else:
        primary_path = to_path
    primary_path = os.path.abspath(os.path.expanduser(primary_path))
    directory = os.path.dirname(primary_path) or "."

    _atomic_write_bytes(primary_bytes, primary_path)

    resources = []
    for name, data in resource_items:
        res_path = os.path.join(directory, name)
        _atomic_write_bytes(data, res_path)
        resources.append({"name": name, "path": res_path, "bytes": len(data),
                          "sha256": hashlib.sha256(data).hexdigest()})

    return {"format": format, "mimetype": mimetype, "extension": extension,
            "path": primary_path, "bytes": len(primary_bytes),
            "sha256": hashlib.sha256(primary_bytes).hexdigest(),
            "bundle": bundle, "resources": resources}


def _build_success(response, format, document_stem, to_path):
    body = response.content
    if not body:
        raise ExportError(
            "the server returned an empty document for format {!r}".format(format),
            status=response.status_code, url=response.url, format=format)

    ctype = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
    bundle = ctype == "application/zip"

    if bundle:
        primary_name, primary_bytes, resource_items = _unpack_bundle(
            body, format, document_stem)
        mimetype = _resource_mimetype(primary_name)
        extension = (EXTENSIONS.get(format) or mimetypes.guess_extension(mimetype)
                    or os.path.splitext(primary_name)[1] or "")
    else:
        primary_bytes = body
        resource_items = []
        mimetype = ctype
        extension = EXTENSIONS.get(format) or mimetypes.guess_extension(ctype) or ""

    if to_path:
        return _write_to_path(format, mimetype, extension, primary_bytes,
                              resource_items, document_stem, to_path, bundle)
    return _inline_result(format, mimetype, extension, primary_bytes,
                          resource_items, bundle)


def _raise_for_status(response, format, is_get, server_path):
    status = response.status_code
    if 300 <= status < 400:
        raise ExportError(
            "authentication failed: the server redirected to its login "
            "page; check the token", status=status, url=response.url,
            format=format)
    if status == 404:
        if is_get:
            message = "notebook not found on the server: {}".format(server_path)
        else:
            message = "this server does not serve the nbconvert endpoints"
        raise ExportError(message, status=404, url=response.url, format=format)
    if status == 403:
        raise ExportError("not authorized to export on this server",
                          status=403, url=response.url, format=format)
    if status == 500:
        match = _TRACEBACK_RE.search(response.text)
        if match:
            detail = _unescape_match(match)
            prefix = "nbconvert failed: "
            if detail.startswith(prefix):
                detail = detail[len(prefix):]
            raise ExportError(detail, status=500, url=response.url,
                              format=format, hint=TOOLCHAIN_HINTS.get(format))
        # No traceback block: either an unknown format or a bare crash.
        # One follow-up probe tells them apart without slowing the happy path.
        return "probe_unknown_format"
    if status >= 400:
        raise ExportError(_error_detail(response), status=status,
                          url=response.url, format=format)
    return None


def _handle_ambiguous_500(client, response, format):
    probe = list_export_formats(client)
    formats = probe.get("formats") or {}
    if format not in formats:
        raise ExportError(
            "unknown export format {!r}".format(format), status=500,
            url=response.url, format=format, available_formats=sorted(formats))
    match = _H1_RE.search(response.text)
    message = _unescape_match(match) if match else _error_detail(response)
    raise ExportError(message, status=500, url=response.url, format=format,
                      hint=TOOLCHAIN_HINTS.get(format))


# ------------------------------------------------------------------- export

def export_notebook(client, format=None, *, server_path=None, cells=None,
                    notebook=None, name=None, to_path=None,
                    include_outputs=None, sanitize_html=None, timeout=None):
    """Export a notebook through the server's nbconvert endpoint.

    Exactly one of three sources is required:

    - ``server_path``: export a notebook already saved in the server's
      contents namespace (``GET /nbconvert/{format}/{path}``). The server
      exports the file exactly as stored — outputs included only if the file
      has them, since ``write_notebook`` doesn't persist outputs by default.
    - ``cells``: build an in-memory notebook from the same cell-spec
      vocabulary ``write_notebook`` accepts, and export it
      (``POST /nbconvert/{format}``) without touching the server's disk.
    - ``notebook``: export an existing nbformat dict (e.g. from
      ``read_notebook`` or a raw ``.ipynb`` ``json.load``) the same way.

    ``include_outputs`` (POST modes only) defaults to ``True`` here —
    opposite of ``write_notebook``, since an export with no results is
    nearly useless. Pass ``False`` to strip ``outputs``/``execution_count``
    from code cells first.

    ``sanitize_html`` only applies to ``server_path`` (the server offers it
    on GET only).

    A zip response (markdown-with-images, for example) is always unpacked:
    the primary document comes back as ``content``, and every sidecar file
    as an entry in ``resources``, keyed by the basename the document
    references it by.

    With ``to_path``, the primary document (and any resources, alongside it)
    are written to disk instead of returned inline; the result then carries
    ``path``/``bytes``/``sha256`` per file instead of ``content``/``encoding``.
    Writes are atomic (temp file + ``os.replace``) and overwrite same-named
    files.

    Raises :class:`ExportError` (a :class:`JupyterError`) for anything the
    server can't produce, with the server's own message recovered from its
    HTML error page rather than the page itself.
    """
    if not format or not isinstance(format, str):
        raise JupyterError("missing required param: format")

    sources = [value for value in (server_path, cells, notebook)
               if value is not None]
    if len(sources) != 1:
        raise JupyterError("pass exactly one of server_path, cells or notebook")

    if sanitize_html is not None and server_path is None:
        raise JupyterError(
            "sanitize_html is only available with server_path (the server "
            "offers it on GET only)")

    if include_outputs is not None and server_path is not None:
        raise JupyterError(
            "include_outputs does not apply to server_path: the server "
            "exports the file as it is stored")

    effective_timeout = timeout if timeout is not None else client.export_timeout
    document_stem = _document_stem(server_path, name)

    if server_path is not None:
        path = "/nbconvert/" + format + "/" + urllib.parse.quote(
            server_path.strip("/"), safe="/")
        params = {}
        if sanitize_html is not None:
            params["sanitize_html"] = "true" if sanitize_html else "false"
        response = client._request_raw(
            "GET", path, params=params or None, timeout=effective_timeout)
    else:
        effective_include_outputs = (
            True if include_outputs is None else include_outputs)
        if cells is not None:
            nbformat = _nbformat()
            nb = nbformat.v4.new_notebook()
            specs = _validate_specs(cells, effective_include_outputs)
            _merge_cells(nb, specs, effective_include_outputs)
            content = nb
        else:
            content = _normalize_raw_notebook(notebook, effective_include_outputs)
        request_name = name or "notebook.ipynb"
        response = client._request_raw(
            "POST", "/nbconvert/" + format,
            json_body={"name": request_name, "content": content},
            timeout=effective_timeout)

    outcome = _raise_for_status(response, format, server_path is not None,
                               server_path)
    if outcome == "probe_unknown_format":
        _handle_ambiguous_500(client, response, format)

    return _build_success(response, format, document_stem, to_path)
