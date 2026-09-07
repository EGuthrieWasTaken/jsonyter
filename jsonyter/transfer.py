"""Chunked file transfer between the local disk and the Jupyter server.

The pipeline ``jupyter server <-> jsonyter <-> editor`` moves code and notebook
source freely but not data files. This module adds that: ``upload`` and
``download`` copy a single file across, in Python, so the editor never has to
hold a multi-hundred-megabyte base64 string — only progress counters cross the
JSON pipe.

Two transports, chosen per direction because the constraints are not
symmetric:

* **Upload** — the Contents API with chunking. ``PUT /api/contents/<path>``
  with a ``chunk`` key, honoured by the default ``AsyncLargeFileManager``:
  ``1`` truncates, ``2..n`` append, ``-1`` appends the last piece and runs
  post-save hooks. Chunking is mandatory: a gateway such as Cloudflare caps
  the request body at 100 MB by default, and a base64 body is 4/3 the raw
  size, so the safe ceiling is ~74 MB (:data:`MAX_SAFE_CHUNK_SIZE`). The
  default :data:`DEFAULT_CHUNK_SIZE` of 8 MiB sits well below that so memory,
  progress granularity and the retry unit all stay small.

* **Download** — ranged ``GET /files/<path>``. A whole-file
  ``GET /api/contents`` base64-encodes and JSON-serialises the entire file
  before the first byte leaves the server, which can trip a proxy's
  time-to-first-byte limit; ``/files/`` streams raw bytes and honours
  ``Range:``, dodging both that and the 33% base64 tax, and it resumes. When
  ``/files/`` will not serve a ``206`` (a non-file-backed contents manager),
  the code falls back to the Contents API with ``format=base64``.

Both directions verify with a hash at the end
(``GET /api/contents/<path>?content=0&hash=1``, jupyter_server >= 2.11); on an
older server they degrade to a size comparison and report ``verified: "size"``
instead of ``"sha256"``. Resume is only ever an optimisation — the hash is
the correctness argument.

This module knows nothing about the JSON stdio protocol: it takes a
``progress`` callable and calls it with plain dicts. Rate-limiting the
progress stream is the caller's job (see :mod:`jsonyter.cli`).
"""

import base64
import hashlib
import os
import posixpath
import time
import urllib.parse
import uuid

from .client import JupyterError, _PROXY_STATUS

DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024
MAX_SAFE_CHUNK_SIZE = 74 * 1024 * 1024   # 100 MB body / (4/3), with headroom

# Bytes pulled per ranged request on the download side. No proxy body limit
# applies to responses, so this is only about progress granularity and memory.
DOWNLOAD_RANGE_SIZE = DEFAULT_CHUNK_SIZE

_IO_BLOCK = 1024 * 1024

# ``os.getcwd()`` / touch / remove, per kernel language. ``{name}`` is
# str-formatted with the sentinel's basename, embedded in a double-quoted
# literal — valid in all three languages, and the sentinel charset (hex plus
# ``.-``) contains no quote, backslash or ``$`` to escape. (Julia in
# particular rejects the single quotes ``{name!r}`` would produce: ``'...'``
# is a character literal there.)
KERNEL_CWD_SNIPPETS = {
    "python": ('import os as _jn_o; print(_jn_o.getcwd())',
               'import pathlib as _jn_p; _jn_p.Path("{name}").touch()',
               'import os as _jn_o; _jn_o.remove("{name}")'),
    "r":      ('cat(getwd())',
               'file.create("{name}")',
               'invisible(file.remove("{name}"))'),
    "julia":  ('print(pwd())',
               'touch("{name}")',
               'rm("{name}")'),
}
_LANG_ALIASES = {"python3": "python", "ipython": "python", "ir": "r"}


class TransferConflict(JupyterError):
    """The destination is not in the state the transfer needs.

    Sibling of :class:`~jsonyter.notebook.NotebookConflict`, with the same
    ``path`` / ``expected`` / ``actual`` shape plus a ``reason``
    discriminator so a front end can pick the right recovery:

    * ``"exists"`` — the destination is already there and would be clobbered;
      pass ``overwrite`` (or ``resume`` to continue a partial upload).
    * ``"stale"`` — it changed on the server since the caller read it (the
      ``expect_hash`` guard fired); re-read it, or drop the guard.
    * ``"corrupt"`` — the bytes that landed do not match the source; retry
      or resume.
    """

    def __init__(self, message, path=None, expected=None, actual=None,
                 reason=None):
        super().__init__(message)
        self.path = path
        self.expected = expected
        self.actual = actual
        self.reason = reason

    def to_json(self):
        payload = super().to_json()
        payload.update({"path": self.path, "expected_hash": self.expected,
                        "actual_hash": self.actual, "reason": self.reason})
        return payload


# --------------------------------------------------------------------- helpers

def _resolve_local(path):
    if not path or not isinstance(path, str):
        raise JupyterError("missing or invalid param: local_path")
    return os.path.abspath(os.path.expanduser(path))


def _clean_remote(path):
    if not isinstance(path, str) or not path.strip("/"):
        raise JupyterError("missing or invalid param: remote_path")
    return path.strip("/")


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(_IO_BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()


def _short(digest):
    if not digest:
        return "?"
    return digest[:8] + "…" if len(digest) > 8 else digest


def _human_size(n):
    if n is None:
        return "unknown size"
    if n < 1024:
        return "{} B".format(n)
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024 or unit == "TB":
            return "{:.1f} {}".format(n, unit)


def _quote(path):
    return urllib.parse.quote(path, safe="/")


def _transfer_timeout(client):
    return getattr(client, "transfer_timeout", None)


def _describe_http_error(response):
    """A phrase saying whether an error came from the proxy or the origin."""
    ray = response.headers.get("cf-ray")
    server = (response.headers.get("server") or "").lower()
    body_head = ""
    try:
        body_head = (response.text or "")[:2000].lower()
    except Exception:      # pragma: no cover - streamed/again-unreadable body
        pass
    by_proxy = bool(ray) and response.status_code in _PROXY_STATUS and (
        "cloudflare" in server or "cloudflare" in body_head)
    if by_proxy:
        return ("HTTP {} from the proxy (cf-ray {} present), not from the "
                "Jupyter server".format(response.status_code, ray))
    reason = response.reason or ""
    return "HTTP {} {}".format(response.status_code, reason).strip()


def _transfer_failure(exc, verb, remote, done, total, *, chunk=None,
                      chunks=None, chunk_size=None, part=None):
    """Re-raise a mid-transfer :class:`JupyterError` with the numbers and the
    recovery spelled out, per the spec's error rules."""
    where = ""
    if chunk is not None and chunks is not None:
        where = " at chunk {}/{}".format(chunk, chunks)
    elif done:
        where = " at byte {}".format(done)
    size = " ({} of {} written)".format(_human_size(done), _human_size(total))
    recover = "resume from byte {}".format(done)
    if chunk_size is not None:
        recover = ("lower --chunk-size (currently {}, ceiling ~74 MB), then "
                   .format(_human_size(chunk_size)) + recover)
    if part is not None:
        recover = "the partial file is kept at {}; re-run with resume".format(
            part)
    raise JupyterError(
        "{} of {} failed{}{} — {} — {}".format(
            verb, remote, where, size, exc.message, recover),
        status=exc.status, url=exc.url, cf_ray=exc.cf_ray) from exc


def _emit(progress, phase, remote, local, done, total, chunk, chunks, started):
    if progress is None:
        return
    progress({
        "phase": phase,
        "path": remote,
        "local_path": local,
        "bytes_done": done,
        "bytes_total": total,
        "chunk": chunk,
        "chunks_total": chunks,
        "elapsed": round(time.monotonic() - started, 3),
    })


# --------------------------------------------------------------------- upload

def _preflight(client, remote, want_hash):
    try:
        return client.get_contents(remote, content=False, hash=want_hash,
                                   timeout=_transfer_timeout(client))
    except JupyterError as exc:
        if exc.status == 404:
            return None
        raise


def _plan_resume(model, chunk_size, bytes_total):
    """(resume_from_byte, note) per the spec's resume rules.

    The server appends blindly, so a partial upload can be continued only if
    every landed chunk is whole — i.e. the server-side size is an exact
    multiple of ``chunk_size`` and short of the full file. Anything else
    restarts from scratch, and ``note`` says why.
    """
    size = int(model.get("size") or 0)
    if size <= 0:
        return 0, None
    if size >= bytes_total:
        return 0, ("restarted from the beginning: the server already holds "
                   "{} byte(s), at or past this file's {} — treating it as a "
                   "stale partial and re-sending in full".format(
                       size, bytes_total))
    if size % chunk_size != 0:
        return 0, ("restarted from the beginning: the server file is {} "
                   "byte(s), not a multiple of the {}-byte chunk size, so a "
                   "chunk landed partially and cannot be safely appended "
                   "to".format(size, chunk_size))
    return size, None


def _verify_upload(client, remote, local_hash, local_size):
    try:
        model = client.get_contents(remote, content=False, hash=True,
                                    timeout=_transfer_timeout(client))
    except JupyterError as exc:
        # Hashing is O(size) on the server and itself subject to the proxy
        # read timeout for very large files — an unanswered verification is
        # "unverified", not a failed transfer.
        if exc.status is None or exc.status in _PROXY_STATUS:
            return "unverified", None, "sha256"
        raise
    server_hash = model.get("hash")
    algo = model.get("hash_algorithm") or "sha256"
    if server_hash and algo == "sha256":
        if server_hash != local_hash:
            raise TransferConflict(
                "{} was uploaded but the server's sha256 ({}) does not match "
                "the local file's ({}) — the bytes that landed are wrong; "
                "retry, or pass resume".format(
                    remote, _short(server_hash), _short(local_hash)),
                path=remote, expected=local_hash, actual=server_hash,
                reason="corrupt")
        return "sha256", server_hash, "sha256"
    server_size = model.get("size")
    if server_size is not None and server_size != local_size:
        raise TransferConflict(
            "{} was uploaded but the server reports {} byte(s), not {} — the "
            "upload is incomplete; retry or pass resume".format(
                remote, server_size, local_size),
            path=remote, expected=str(local_size), actual=str(server_size),
            reason="corrupt")
    return "size", server_hash, algo


def upload(client, local_path, remote_path, chunk_size=DEFAULT_CHUNK_SIZE,
           overwrite=False, expect_hash=None, resume=False, progress=None):
    """Copy a local file to the Jupyter server, chunked.

    ``chunk_size`` is the raw bytes per ``PUT`` (default 8 MiB); anything
    over :data:`MAX_SAFE_CHUNK_SIZE` is rejected before a byte is sent, since
    a silent ``413`` halfway through a multi-GB upload is a bad failure mode.

    The destination is refused with :class:`TransferConflict` when it already
    exists and neither ``overwrite`` nor a matching ``expect_hash`` was
    given; with ``resume=True`` a whole-chunk partial is continued instead.
    ``expect_hash`` is the sha256 the caller last saw for the destination — a
    mismatch raises ``reason="stale"`` rather than clobbering a file that
    moved under them.

    Returns ``{"path", "local_path", "bytes", "chunks", "hash",
    "hash_algorithm", "verified", "resumed_at", "resume_note", "elapsed"}``.
    ``verified`` is ``"sha256"``, ``"size"`` (old server) or ``"unverified"``
    (the hash request timed out).
    """
    started = time.monotonic()
    local = _resolve_local(local_path)
    remote = _clean_remote(remote_path)

    if not isinstance(chunk_size, int) or isinstance(chunk_size, bool) \
            or chunk_size <= 0:
        raise JupyterError("chunk_size must be a positive integer")
    if chunk_size > MAX_SAFE_CHUNK_SIZE:
        raise JupyterError(
            "chunk_size {} exceeds the safe maximum of {} bytes (~74 MB): a "
            "base64 request body is 4/3 of the raw chunk, and Cloudflare "
            "rejects bodies over 100 MB by default (200 MB Business, up to "
            "5 GB Enterprise, adjustable in the zone's Network settings). "
            "Lower --chunk-size.".format(chunk_size, MAX_SAFE_CHUNK_SIZE))

    if not os.path.isfile(local):
        raise JupyterError("no such file: {}".format(local))
    bytes_total = os.path.getsize(local)
    chunks_total = max(1, (bytes_total + chunk_size - 1) // chunk_size)

    resume_from = 0
    resume_note = None
    existing = _preflight(client, remote, want_hash=expect_hash is not None)
    if existing is not None:
        if existing.get("type") == "directory":
            raise JupyterError(
                "{} is a directory on the server, not a file".format(remote))
        server_hash = existing.get("hash")
        server_size = existing.get("size")
        modified = existing.get("last_modified")
        when = ", modified " + modified if modified else ""

        if (expect_hash is not None and server_hash is not None
                and server_hash != expect_hash):
            raise TransferConflict(
                "{} changed on the server since you read it (expected sha256 "
                "{}, found {}{}) — download it first, or pass overwrite to "
                "replace it".format(remote, _short(expect_hash),
                                    _short(server_hash), when),
                path=remote, expected=expect_hash, actual=server_hash,
                reason="stale")

        authorized = overwrite or (
            expect_hash is not None and server_hash is not None
            and server_hash == expect_hash)
        if resume and not authorized:
            resume_from, resume_note = _plan_resume(
                existing, chunk_size, bytes_total)
            authorized = True
        if not authorized:
            why = ""
            if expect_hash is not None and server_hash is None:
                why = (" — this server does not report content hashes, so "
                       "expect_hash could not be checked")
            raise TransferConflict(
                "{} already exists on the server ({}{}){} — pass overwrite to "
                "replace it, or resume to continue a partial upload".format(
                    remote, _human_size(server_size), when, why),
                path=remote, expected=expect_hash, actual=server_hash,
                reason="exists")

    first_chunk = resume_from // chunk_size + 1 if resume_from else 1
    timeout = _transfer_timeout(client)
    bytes_done = resume_from
    # Hash while streaming — one sequential read of the file, not two.
    digest = hashlib.sha256()
    with open(local, "rb") as handle:
        remaining = resume_from
        while remaining > 0:                    # feed the already-sent prefix
            block = handle.read(min(_IO_BLOCK, remaining))
            if not block:                      # file shrank under us
                raise JupyterError(
                    "{} is shorter than the {} bytes already on the server; "
                    "re-run without resume".format(local, resume_from))
            digest.update(block)
            remaining -= len(block)
        index = first_chunk
        while True:
            block = handle.read(chunk_size)
            if not block and bytes_total != 0:
                break
            digest.update(block)
            is_last = index >= chunks_total
            if chunks_total == 1:
                label = None                     # a lone chunk:1 skips hooks
            elif is_last:
                label = -1
            else:
                label = index
            try:
                client.put_contents(
                    remote, base64.b64encode(block).decode("ascii"),
                    type="file", format="base64", chunk=label, timeout=timeout)
            except JupyterError as exc:
                _transfer_failure(exc, "upload", remote, bytes_done, bytes_total,
                                  chunk=index, chunks=chunks_total,
                                  chunk_size=chunk_size)
            bytes_done += len(block)
            _emit(progress, "upload", remote, local, bytes_done, bytes_total,
                  index, chunks_total, started)
            index += 1
            if is_last or bytes_total == 0:
                break

    local_hash = digest.hexdigest()
    verified, _server_hash, algo = _verify_upload(
        client, remote, local_hash, bytes_total)
    _emit(progress, "upload", remote, local, bytes_total, bytes_total,
          chunks_total, chunks_total, started)
    return {
        "path": remote,
        "local_path": local,
        "bytes": bytes_total,
        "chunks": chunks_total,
        "hash": local_hash,
        "hash_algorithm": algo,
        "verified": verified,
        "resumed_at": resume_from,
        "resume_note": resume_note,
        "elapsed": round(time.monotonic() - started, 3),
    }


# ------------------------------------------------------------------- download

def _ranged_download(session, url, part, total, resume, progress, started,
                     remote, local, timeout):
    offset = 0
    mode = "wb"
    if resume and os.path.exists(part):
        have = os.path.getsize(part)
        if total is not None and have >= total:
            offset = 0                           # local .part outran the remote
        else:
            offset, mode = have, "ab"
    if mode == "wb" and os.path.exists(part):
        os.remove(part)
    resumed_at = offset

    span = DOWNLOAD_RANGE_SIZE
    chunks_total = ((total + span - 1) // span) if total else None
    with open(part, mode) as out:
        while total is None or offset < total:
            end = offset + span - 1
            if total is not None:
                end = min(end, total - 1)
            resp = session.get(
                url, headers={"Range": "bytes={}-{}".format(offset, end)},
                stream=True, timeout=timeout, allow_redirects=False)
            try:
                if resp.status_code == 200 and offset > 0:
                    # The server honoured the probe's Range but not this one:
                    # its body is the whole file, and appending it to a
                    # partial .part would corrupt it. Bail with a clean
                    # recovery rather than poison the file.
                    raise JupyterError(
                        "download of {} failed at byte {} of {}: the server "
                        "answered a mid-stream Range request with the whole "
                        "file (HTTP 200) — re-run without resume".format(
                            remote, offset, total),
                        status=200, url=url)
                if resp.status_code not in (200, 206):
                    _transfer_failure(
                        JupyterError(_describe_http_error(resp),
                                     status=resp.status_code, url=url,
                                     cf_ray=resp.headers.get("cf-ray")),
                        "download", remote, offset, total, part=part)
                got = 0
                for block in resp.iter_content(chunk_size=_IO_BLOCK):
                    if not block:
                        continue
                    out.write(block)
                    offset += len(block)
                    got += len(block)
                    idx = (offset + span - 1) // span
                    _emit(progress, "download", remote, local, offset, total,
                          idx, chunks_total, started)
            finally:
                resp.close()
            if resp.status_code == 200:      # whole body arrived at offset 0
                break
            if got == 0:            # server sent nothing — stop rather than spin
                break
    return "files", resumed_at, None, (chunks_total or 1)


def _stream_download(session, url, part, total, progress, started, remote,
                     local, timeout):
    if os.path.exists(part):
        os.remove(part)
    resp = session.get(url, stream=True, timeout=timeout, allow_redirects=False)
    offset = 0
    try:
        if resp.status_code != 200:
            _transfer_failure(
                JupyterError(_describe_http_error(resp),
                             status=resp.status_code, url=url,
                             cf_ray=resp.headers.get("cf-ray")),
                "download", remote, 0, total, part=part)
        with open(part, "wb") as out:
            for block in resp.iter_content(chunk_size=_IO_BLOCK):
                if not block:
                    continue
                out.write(block)
                offset += len(block)
                _emit(progress, "download", remote, local, offset, total,
                      1, 1, started)
    finally:
        resp.close()
    return "files", 0, ("the server ignored Range and sent the whole file; "
                        "downloaded without resume support"), 1


def _contents_download(client, remote, part, progress, started, local):
    model = client.get_contents(remote, content=True, type="file",
                                format="base64",
                                timeout=_transfer_timeout(client))
    payload = model.get("content") or ""
    if model.get("format") == "base64":
        data = base64.b64decode(payload)
    else:                                        # text model — re-encode
        data = payload.encode("utf-8")
    if os.path.exists(part):
        os.remove(part)
    with open(part, "wb") as out:
        out.write(data)
    _emit(progress, "download", remote, local, len(data), len(data), 1, 1,
          started)
    return "contents", 0, ("downloaded via the Contents API (base64); "
                           "/files/ did not serve a ranged response"), 1


def _download_bytes(client, remote, part, remote_size, resume, progress,
                    started, local):
    url = client.base_url + "/files/" + _quote(remote)
    session = client._http
    timeout = _transfer_timeout(client)

    probe = session.get(url, headers={"Range": "bytes=0-0"}, stream=True,
                        timeout=timeout, allow_redirects=False)
    try:
        status = probe.status_code
        content_range = probe.headers.get("Content-Range") or ""
    finally:
        probe.close()

    if 300 <= status < 400:
        raise JupyterError(
            "authentication failed: /files/{} redirected to a login page; "
            "check the token".format(remote), status=status, url=url)

    total = remote_size
    if status == 206 and "/" in content_range:
        try:
            total = int(content_range.rsplit("/", 1)[1])
        except ValueError:
            pass

    if status == 206:
        return _ranged_download(session, url, part, total, resume, progress,
                                started, remote, local, timeout)
    if status == 200:
        return _stream_download(session, url, part, total, progress, started,
                                remote, local, timeout)
    return _contents_download(client, remote, part, progress, started, local)


def download(client, remote_path, local_path, overwrite=False, expect_hash=None,
             resume=False, progress=None):
    """Copy a file from the Jupyter server to local disk.

    Tries ranged ``/files/`` first and falls back to the Contents API
    (base64) when the server will not serve a ``206``. Bytes land in
    ``local_path + ".part"`` and are ``os.replace``\\ d onto ``local_path``
    only after the hash checks out, so an interrupted download never leaves a
    truncated file at the real name.

    ``expect_hash`` is the sha256 the caller last saw for the *remote* file;
    a mismatch raises :class:`TransferConflict` (``reason="stale"``).
    ``overwrite`` is required to replace an existing local file.

    Returns the same shape as :func:`upload` plus ``"transport"``
    (``"files"`` or ``"contents"``).
    """
    started = time.monotonic()
    remote = _clean_remote(remote_path)
    local = _resolve_local(local_path)
    part = local + ".part"

    try:
        model = client.get_contents(remote, content=False, hash=True,
                                    timeout=_transfer_timeout(client))
    except JupyterError as exc:
        if exc.status == 404:
            raise JupyterError(
                "no such file on the server: {}".format(remote))
        if exc.status is not None and exc.status not in _PROXY_STATUS:
            raise
        # Hashing re-reads the whole file server-side and can trip the proxy
        # read timeout; retry without it to at least learn the size.
        model = client.get_contents(remote, content=False,
                                    timeout=_transfer_timeout(client))

    if model.get("type") == "directory":
        raise JupyterError(
            "{} is a directory — only single files can be downloaded".format(
                remote))
    remote_hash = model.get("hash")
    remote_algo = model.get("hash_algorithm") or "sha256"
    remote_size = model.get("size")
    modified = model.get("last_modified")

    if (expect_hash is not None and remote_hash is not None
            and remote_hash != expect_hash):
        raise TransferConflict(
            "{} changed on the server since you read it (expected sha256 {}, "
            "found {}{}) — re-read it, or drop expect_hash to take the "
            "current version".format(
                remote, _short(expect_hash), _short(remote_hash),
                ", modified " + modified if modified else ""),
            path=remote, expected=expect_hash, actual=remote_hash,
            reason="stale")

    if os.path.exists(local) and not overwrite:
        raise TransferConflict(
            "{} already exists locally ({}) — pass overwrite to replace "
            "it".format(local, _human_size(os.path.getsize(local))),
            path=local, reason="exists")

    transport, resumed_at, note, n_chunks = _download_bytes(
        client, remote, part, remote_size, resume, progress, started, local)

    local_hash = _sha256_file(part)
    actual_size = os.path.getsize(part)
    if remote_hash and remote_algo == "sha256":
        if local_hash != remote_hash:
            raise TransferConflict(
                "{} downloaded to {} but its sha256 ({}) does not match the "
                "server's ({}) — the .part file has been kept for "
                "inspection; retry, or pass resume".format(
                    remote, part, _short(local_hash), _short(remote_hash)),
                path=local, expected=remote_hash, actual=local_hash,
                reason="corrupt")
        verified = "sha256"
    elif remote_size is not None:
        if actual_size != remote_size:
            raise TransferConflict(
                "{} downloaded but is {} byte(s), not the server's {} — the "
                ".part file has been kept; retry or pass resume".format(
                    remote, actual_size, remote_size),
                path=local, expected=str(remote_size),
                actual=str(actual_size), reason="corrupt")
        verified = "size"
    else:
        verified = "unverified"

    os.replace(part, local)
    _emit(progress, "download", remote, local, actual_size, actual_size,
          n_chunks, n_chunks, started)
    return {
        "path": remote,
        "local_path": local,
        "bytes": actual_size,
        "chunks": n_chunks,
        "hash": local_hash,
        "hash_algorithm": remote_algo,
        "verified": verified,
        "resumed_at": resumed_at,
        "resume_note": note,
        "transport": transport,
        "elapsed": round(time.monotonic() - started, 3),
    }


# ------------------------------------------------------- kernel contents dir

_PROBE_TIMEOUT = 30.0    # a busy kernel must not wedge a REST worker forever


def _run_code(conn, code):
    """Run a one-liner and return its stdout / repr text, stripped.

    Bounded: the probe rides the kernel's shell queue, so a kernel already
    deep in a long ``execute`` would otherwise block a REST worker
    indefinitely — a timeout there degrades to "unresolved", which is the
    right answer anyway.
    """
    result = conn.execute(code, silent=False, store_history=False,
                          timeout=_PROBE_TIMEOUT)
    parts = []
    for out in result.get("outputs", []):
        kind = out.get("type")
        if kind == "stream" and out.get("name") == "stdout":
            parts.append(out.get("text") or "")
        elif kind in ("execute_result", "display_data"):
            text = (out.get("data") or {}).get("text/plain")
            if text:
                parts.append(text)
        elif kind == "error":
            raise JupyterError("kernel error while probing: {}: {}".format(
                out.get("ename"), out.get("evalue")))
    return "".join(parts).strip()


def _resolve_suffix(client, cwd, sentinel):
    """Longest-first suffix of ``cwd`` whose listing contains ``sentinel``.

    Suffix matching alone is ambiguous — two directories can both resolve —
    so the sentinel file is what makes the answer exact. Returns
    ``(contents_dir, root_dir)`` or ``None``.
    """
    parts = [p for p in cwd.strip("/").split("/") if p]
    candidates = ["/".join(parts[i:]) for i in range(len(parts))]
    candidates.append("")                        # the contents root itself
    stripped_cwd = cwd.rstrip("/")
    for cand in candidates:
        try:
            listing = client.list_contents(
                cand, timeout=_transfer_timeout(client))
        except JupyterError:
            continue
        children = listing.get("content")
        if not isinstance(children, list):
            continue
        if not any(child.get("name") == sentinel for child in children):
            continue
        if cand == "":
            return "", stripped_cwd or "/"
        if stripped_cwd == cand:
            return cand, "/"
        if stripped_cwd.endswith("/" + cand):
            return cand, stripped_cwd[: -len("/" + cand)] or "/"
        return cand, None                        # suffix matched but not a tail
    return None


def kernel_contents_dir(client, kernel_id, root=None, conn=None):
    """Map a kernel's working directory to a Contents-API path.

    Contents paths (relative to the server's ``root_dir``, no leading slash)
    and the kernel's absolute cwd are different coordinate systems, and no
    API reports ``root_dir``. This learns the mapping so an "upload here"
    default can be correct:

    1. ask the kernel for its cwd;
    2. have it drop an empty ``.jsonyter-probe-<uuid>`` there;
    3. walk the cwd's path suffixes longest-first, listing each, and take
       the first listing that actually contains the sentinel;
    4. have the kernel delete the sentinel.

    Returns ``{"kernel_id", "cwd", "contents_dir", "root_dir", "method",
    "language"}``. ``method`` is ``"probe"``, ``"configured"`` (an explicit
    ``root`` was given, so the probe is skipped), ``"unresolved"`` (no suffix
    resolved — the kernel is outside ``root_dir``; ``contents_dir`` is
    ``None`` rather than a guess) or ``"unsupported"`` (no snippet for the
    kernel's language — also not an error).

    ``conn`` is an existing :class:`~jsonyter.kernel.KernelConnection` to
    reuse; without one a short-lived connection is opened and closed.
    """
    own_conn = conn is None
    if own_conn:
        conn = client.kernel(kernel_id)
    try:
        return _kernel_contents_dir(client, kernel_id, conn, root)
    finally:
        if own_conn:
            try:
                conn.close()
            except Exception:
                pass


def _kernel_contents_dir(client, kernel_id, conn, root):
    try:
        info = conn.kernel_info()
        language = ((info or {}).get("language_info") or {}).get("name") or ""
    except JupyterError:
        language = ""
    key = _LANG_ALIASES.get(language.lower(), language.lower())
    snippets = KERNEL_CWD_SNIPPETS.get(key)

    cwd = None
    if snippets is not None:
        try:
            cwd = _run_code(conn, snippets[0]) or None
        except JupyterError:
            cwd = None

    if root is not None:
        root_norm = root.rstrip("/") or "/"
        contents_dir = None
        if cwd is not None:
            rel = posixpath.relpath(cwd, root_norm)
            contents_dir = "" if rel == "." else (
                None if rel.startswith("..") else rel)
        return {"kernel_id": kernel_id, "cwd": cwd,
                "contents_dir": contents_dir, "root_dir": root_norm,
                "method": "configured", "language": language or None}

    if snippets is None:
        return {"kernel_id": kernel_id, "cwd": None, "contents_dir": None,
                "root_dir": None, "method": "unsupported",
                "language": language or None}
    if cwd is None:
        return {"kernel_id": kernel_id, "cwd": None, "contents_dir": None,
                "root_dir": None, "method": "unresolved",
                "language": language or None}

    sentinel = ".jsonyter-probe-" + uuid.uuid4().hex
    try:
        _run_code(conn, snippets[1].format(name=sentinel))
    except JupyterError:
        return {"kernel_id": kernel_id, "cwd": cwd, "contents_dir": None,
                "root_dir": None, "method": "unresolved",
                "language": language or None}
    try:
        resolved = _resolve_suffix(client, cwd, sentinel)
    finally:
        try:
            _run_code(conn, snippets[2].format(name=sentinel))
        except JupyterError:
            pass

    if resolved is None:
        return {"kernel_id": kernel_id, "cwd": cwd, "contents_dir": None,
                "root_dir": None, "method": "unresolved",
                "language": language or None}
    contents_dir, root_dir = resolved
    return {"kernel_id": kernel_id, "cwd": cwd, "contents_dir": contents_dir,
            "root_dir": root_dir, "method": "probe",
            "language": language or None}
