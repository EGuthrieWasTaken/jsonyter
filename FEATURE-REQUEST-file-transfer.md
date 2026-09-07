# Feature request: file transfer between the Emacs client and the Jupyter server

**Status:** proposed
**Date:** 2026-09-07
**Scope:** `jsonyter` (this repo). A companion document in `jsonyter.el`
covers the Emacs side; the two are written to be implemented in that order,
because the elisp is purely a consumer of the protocol defined here.

---

## 1. Problem

The pipeline is `jupyter server <--> jsonyter <--> jsonyter.el`. Code and
notebook source move freely in both directions; **data files do not**. Getting
a CSV from the machine running Emacs onto the machine running the kernel
currently means dropping out of the pipeline entirely — `scp`, `rclone`, the
JupyterLab web UI — none of which know anything about the session, the kernel's
working directory, or the file the user is looking at.

The goal is a transfer path that is *built into the bridge*: no external
binaries, no second set of credentials, no separate transport to configure.

## 2. Why the bridge is the right place

`jsonyter` already straddles both filesystems:

- it runs as a subprocess of Emacs, so it has ordinary local disk access; and
- it is already authenticated against the Jupyter server's REST API, which
  exposes that server's filesystem through the **Contents API**.

So the capability is already latent in the process — `Client.get_contents`
is half of one of the verbs. Nothing new needs to be deployed, and no
credential exists that the bridge doesn't already hold.

Doing the work in Python (rather than in elisp) is not incidental. Emacs must
never hold a multi-hundred-megabyte base64 string; chunking, hashing, and file
I/O all belong on this side of the pipe, with only progress counters crossing
it.

## 3. Deployment constraint: the server is behind Cloudflare

This is the constraint that shapes the whole design, and it must be assumed by
default rather than treated as an edge case.

| Limit | Value | Consequence |
|---|---|---|
| Request body size | **100 MB** (Free/Pro; 200 MB Business, up to 5 GB Enterprise, adjustable in zone Network settings) | Uploads **must** be chunked. Non-negotiable. |
| Proxy read timeout | **125 s** to first response from origin | A whole-file `GET /api/contents` can 524 on a large file, because Jupyter reads + base64-encodes + JSON-serializes the *entire* file before tornado writes a byte. |

Response *size* is not capped, but time-to-first-byte is — which is why the
download path cannot simply be "the upload path in reverse".

## 4. Design decisions

### 4.1 Two transports, chosen per direction

**Uploads — Contents API with chunking.** `PUT /api/contents/<path>` with a
`chunk` key in the model. The default contents manager is
`AsyncLargeFileManager`, which honours it: `chunk: 1` creates/truncates,
`2..n` append, and `chunk: -1` appends the final piece and runs post-save
hooks. This is the same mechanism JupyterLab's own uploader uses.

**Downloads — `/files/<path>` with HTTP `Range`.** With a file-backed contents
manager this route is served by `AuthenticatedFileHandler`, a tornado
`StaticFileHandler` subclass, so it streams raw bytes and honours `Range:`.
Token-authenticated requests bypass the XSRF check, so the existing
`Authorization: token` header is sufficient. This avoids both the 33% base64
tax and the 125 s timeout, and gives resumability.

**Download fallback — Contents API with `format=base64`.** Used when `/files/`
does not return `206` (a non-file-backed contents manager — S3 and friends —
falls through to `FilesHandler`, which does not do Range). Correct but
memory-hungry; acceptable as a fallback because those deployments are rare.

**Rejected for v1: transferring through the kernel.** Base64 chunks pushed
through `execute` would work for any path the kernel can see, including paths
outside the server's `root_dir`, and needs no additional server capability.
It is also slow and needs per-language code. The `transfer.py` seam below
keeps it additive; do not build it now.

### 4.2 Chunk sizing

```
body ≈ (4/3 × raw) + JSON envelope
```

| | |
|---|---|
| Cloudflare ceiling | 100 MB body |
| ⇒ absolute maximum raw chunk | ~74 MB |
| **Default** | **8 MiB raw** (≈10.7 MB body) |
| Jupyter `ServerApp.max_body_size` | 512 MiB default — not the binding constraint |

8 MiB is deliberately far from the ceiling: it bounds memory on both ends,
makes progress reporting meaningful, and keeps the retry unit cheap. Expose it
as `--chunk-size` on the CLI and as a parameter on `upload()`, because the
ceiling moves with the Cloudflare plan.

### 4.3 Resume

The server appends blindly — it does not verify offsets — so a partial upload
can be continued rather than restarted:

1. `GET /api/contents/<path>?content=0` returns `size`.
2. If `size % chunk_size == 0` and `size < total`, resume at chunk
   `size // chunk_size + 1`.
3. If `size % chunk_size != 0`, a chunk landed partially. **Do not resume** —
   restart from `chunk: 1` and say why.

Downloads resume via `Range: bytes=<local_size>-` against a local partial file.

Both directions verify with a hash at the end (§4.4). Resume is an
optimisation, never a correctness argument.

### 4.4 Integrity

`GET /api/contents/<path>?content=0&hash=1` (jupyter_server ≥ 2.11) returns
`{"hash": ..., "hash_algorithm": "sha256"}` **without transferring the file** —
`_file_model` re-reads the bytes server-side when `content=0` but
`require_hash=1`. sha256 is the default and matches `notebook.file_hash`, so
local and remote digests are directly comparable.

Two caveats to handle:

- Servers older than 2.11 ignore the `hash` argument and return a model with
  no `hash` key. Degrade to comparing `size` only, and set
  `"verified": "size"` rather than `"sha256"` in the result so the caller can
  say which guarantee it got.
- Hashing is O(size) *on the server*, so it is itself subject to the 125 s
  proxy timeout for very large files. Treat a timeout on the verification
  request as unverified, not as a failed transfer.

### 4.5 Path semantics

Contents API paths are POSIX-style, relative to the server's `root_dir`, with
no leading slash. The kernel's working directory is a **different coordinate
system**, and no API reports `root_dir`. Three layers, in order:

**(a) Contents paths are the one true coordinate system.** Every method in
this document takes and returns a contents path. Never accept a kernel-side
absolute path where a contents path is expected. The Emacs side surfaces this
explicitly in its prompts.

**(b) A sentinel probe learns the mapping, once per kernel.** So that
"upload here" can default correctly:

1. Ask the kernel for its cwd (`os.getcwd()` in Python, `getwd()` in R,
   `pwd()` in Julia — see `KERNEL_CWD_SNIPPETS` below).
2. Have the kernel write an empty `.jsonyter-probe-<uuid>` in that cwd.
3. Walk the cwd's path suffixes longest-first (`home/x/work/data`,
   `x/work/data`, `work/data`, `data`), calling `list_contents` on each, and
   take the first listing that contains the sentinel. Suffix matching alone is
   ambiguous — two directories can both resolve — and the sentinel is what
   makes the answer exact.
4. Have the kernel delete the sentinel.
5. Cache the result; invalidate on kernel restart.

Return `null` rather than guessing when no suffix resolves: that is the
"kernel is outside `root_dir`" case, and a wrong answer there is worse than no
answer.

**(c) A configured override for when (b) fails.** The Emacs side exposes
`jsonyter-remote-root`; this side just accepts an explicit `root` argument to
`kernel_contents_dir` and skips the probe when given.

### 4.6 Overwrite conflicts

Reuse the existing `NotebookConflict` pattern rather than inventing a second
one. Add a sibling `TransferConflict(JupyterError)` with the same
`path`/`expected`/`actual` shape plus a `reason` discriminator so the front end
can offer the right recovery. See §8.

---

## 5. `client.py` — contents verbs

Thin `_request` wrappers, all `@prettifiable`, all on the REST worker pool.

Add a `_patch` helper alongside `_get`/`_post`/`_delete`.

```python
def get_contents(self, path="", content=True, type=None, format=None,
                 hash=False):
    """GET /api/contents/<path>

    ``format="base64"`` forces byte-exact retrieval of a text file.
    ``hash=True`` adds sha256 to the model (jupyter_server >= 2.11) and
    works with ``content=False`` — the server re-reads the bytes to hash
    them, so the digest costs no bandwidth.
    """

def put_contents(self, path, content, type="file", format="base64",
                 chunk=None):
    """PUT /api/contents/<path>

    ``chunk`` implements the large-file protocol: 1 truncates/creates,
    2..n append, -1 appends the last piece and runs post-save hooks.
    Only ``type="file"`` supports chunking, server-side.
    """

def make_directory(self, path):
    """PUT /api/contents/<path> with {"type": "directory"}.

    Unlike POST, this names the directory exactly; POST returns an
    "Untitled Folder".
    """

def delete_contents(self, path):
    """DELETE /api/contents/<path>."""

def rename_contents(self, path, new_path):
    """PATCH /api/contents/<path> with {"path": new_path}.

    Note the asymmetry: the URL carries the OLD path, the body the NEW
    one. (The handler names its local variable ``old_path`` for the body
    value, which is misleading — read ``ContentsManager.update``.)
    Moves and renames are the same operation.
    """

def copy_contents(self, path, to_dir):
    """POST /api/contents/<to_dir> with {"copy_from": path}.

    Server-side copy: no bytes cross the wire. The server picks the
    destination NAME (appending "-Copy1" and so on); the returned model
    carries the name it chose, so callers must read it rather than assume.
    """

def list_contents(self, path=""):
    """Directory listing: get_contents(path, content=True) on a directory.

    Children come back without content but with name/path/type/size/
    last_modified/writable, which is everything a browser UI needs.
    """
```

## 6. New module: `jsonyter/transfer.py`

Owns chunking, hashing, local file I/O, and progress. Keep it free of
`cli.py` imports — it takes a `progress` callable and knows nothing about the
JSON protocol.

```python
DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024
MAX_SAFE_CHUNK_SIZE = 74 * 1024 * 1024   # 100 MB body / (4/3) with headroom


class TransferConflict(JupyterError):
    """The destination changed, or exists and would be clobbered."""
    def __init__(self, message, path=None, expected=None, actual=None,
                 reason=None): ...
    # to_json() adds path, expected_hash, actual_hash, reason


def upload(client, local_path, remote_path, chunk_size=DEFAULT_CHUNK_SIZE,
           overwrite=False, expect_hash=None, resume=False, progress=None):
    """Local file -> server, chunked.

    Returns {"path", "local_path", "bytes", "chunks", "hash",
             "hash_algorithm", "verified", "resumed_at", "elapsed"}.

    Raises TransferConflict when the destination exists and neither
    ``overwrite`` nor a matching ``expect_hash`` was given.
    """


def download(client, remote_path, local_path, overwrite=False,
             expect_hash=None, resume=False, progress=None):
    """Server -> local file.

    Tries ranged /files/ first; falls back to Contents API base64.
    Writes to ``local_path + ".part"`` and renames on success, so an
    interrupted download never leaves a truncated file at the real name.

    Returns the same shape as upload(), plus "transport": "files"|"contents".
    """
```

### 6.1 Upload algorithm

1. Reject `chunk_size > MAX_SAFE_CHUNK_SIZE` with a message naming the
   Cloudflare limit — a silent 413 halfway through a 3 GB upload is a bad
   failure mode.
2. `stat` the local file for `bytes_total`; compute `chunks_total`.
3. Preflight: `get_contents(remote_path, content=False, hash=bool(expect_hash))`.
   - 404 → proceed.
   - exists, `overwrite=False`, no `expect_hash` → `TransferConflict`
     (`reason="exists"`).
   - exists, `expect_hash` given and mismatched → `TransferConflict`
     (`reason="stale"`).
   - exists, `resume=True` → apply §4.3.
4. Stream the local file in `chunk_size` reads. `base64.b64encode` each.
   `put_contents(..., chunk=k)` with `k = 1, 2, ... n-1`, then `chunk=-1` for
   the final piece.
   - **Single-chunk case:** a file that fits in one chunk should be sent with
     `chunk=None` (a plain save), not `chunk=1` followed by nothing — a lone
     `chunk: 1` never runs post-save hooks.
5. Verify per §4.4. Emit a final progress event.

### 6.2 Download algorithm

1. `get_contents(remote_path, content=False, hash=True)` for `size` and hash.
   Fail early on 404, and refuse a `type == "directory"` model with a clear
   message.
2. Probe `/files/`: `GET {base_url}/files/{path}` with `Range: bytes=0-0`.
   - `206` → ranged transport. Loop `Range: bytes=<offset>-<offset+span-1>`
     with `stream=True` and `iter_content`, appending to the `.part` file.
   - `200` → the server ignored the Range. Stream the whole body anyway
     (still better than base64) but disable resume.
   - `404`/error → Contents API fallback: `get_contents(..., content=True,
     format="base64")`, decode, write.
3. Compare the local digest against the server's. On mismatch, keep the
   `.part` file, delete nothing, and raise with both digests.
4. `os.replace` the `.part` file onto `local_path`.

Use `client._http` (the existing authenticated `requests.Session`) for
`/files/` so token, TLS verification and proxy settings are shared. Do not
construct a second session.

## 7. `cli.py` wiring

Add to `_CLIENT_METHODS` — **not** `_KERNEL_METHODS`. These go on the REST
worker pool for the same reason `read_notebook`/`write_notebook` do: a 400 MB
upload must never queue behind, or ahead of, a running `execute`.

```python
"get_contents":    ("path", "content", "type", "format", "hash"),
"put_contents":    ("path", "content", "type", "format", "chunk"),
"make_directory":  ("path",),
"delete_contents": ("path",),
"rename_contents": ("path", "new_path"),
"copy_contents":   ("path", "to_dir"),
"list_contents":   ("path",),
"upload":          ("local_path", "remote_path", "chunk_size", "overwrite",
                    "expect_hash", "resume"),
"download":        ("remote_path", "local_path", "overwrite", "expect_hash",
                    "resume"),
"kernel_contents_dir": ("kernel_id", "root"),
```

`upload`/`download`/`kernel_contents_dir` are not `Client` methods; dispatch
them explicitly to `transfer.py` / §7.2, passing `self.client`.

### 7.1 Progress lines

A fourth non-final line type, alongside `output`, `input_request` and `event`.
The protocol already dispatches on which key is present, and `jsonyter--dispatch`
needs only one new branch.

```json
{"id": 7, "progress": {"phase": "upload", "path": "data/trials.csv",
                       "local_path": "/home/e/trials.csv",
                       "bytes_done": 25165824, "bytes_total": 193273528,
                       "chunk": 3, "chunks_total": 23, "elapsed": 4.12}}
```

Emit on every chunk boundary, but rate-limit to at most ~4/second so a
fast local transfer of a small-chunked file cannot flood the pipe. Emit one
final event with `bytes_done == bytes_total` before the `result` line.

Document this line type in the `cli` module docstring alongside the others.

### 7.2 `kernel_contents_dir`

Implements §4.5(b). Needs a kernel connection, but is dispatched on the REST
pool and takes `kernel_id` explicitly (it issues one short `execute` through
the existing connection).

```python
KERNEL_CWD_SNIPPETS = {
    "python": ("import os; print(os.getcwd())",
               "import pathlib; pathlib.Path({name!r}).touch()",
               "import os; os.remove({name!r})"),
    "R":      ("cat(getwd())", ...),
    "julia":  ("print(pwd())", ...),
}
```

Returns:

```json
{"kernel_id": "...", "cwd": "/home/jovyan/work/analysis",
 "contents_dir": "work/analysis", "root_dir": "/home/jovyan",
 "method": "probe"}
```

`method` is `"probe"`, `"configured"` (a `root` argument was supplied), or
`"unresolved"` with `contents_dir: null`. Never guess. A kernel whose language
has no snippet returns `"unsupported"`, also with a null `contents_dir` — that
is not an error.

## 8. Errors

Every message must answer "what do I do now?". Three rules:

**Distinguish proxy errors from Jupyter errors.** A 413 or a 524 whose body is
a Cloudflare error page is not the Jupyter server talking, and the fix is a
different knob. Sniff the `cf-ray` response header and say so explicitly. This
extends the existing 401/403 special-casing in `jsonyter--error-message`.

**Name the numbers.** Bytes written, chunk reached, both digests, the current
and the maximum chunk size.

**Name the recovery.** `overwrite`, a lower chunk size, `resume`, a download
first.

Target shapes:

```
data/trials.csv changed on the server since you read it (expected sha256
3f2a…, found 9c11…, modified 2026-09-07T14:22Z) — download it first, or
pass overwrite to replace it
```

```
data/trials.csv already exists on the server (412 KB, modified
2026-09-06T09:31Z) — pass overwrite to replace it
```

```
upload of trials.csv failed at chunk 7/23 (12.4 MB of 184 MB written) —
HTTP 413 from the proxy (cf-ray present), not from the Jupyter server: the
request body exceeded a gateway limit. Lower chunk_size (currently 64 MiB;
Cloudflare's default cap is 100 MB), then resume from byte 13008896.
```

```
download of data/trials.csv verified as size only, not sha256: this
server predates jupyter_server 2.11 and does not return content hashes
```

## 9. Testing

- `upload`/`download` round-trip: random bytes at 0 B, 1 B, exactly
  `chunk_size`, `chunk_size + 1`, and `3 × chunk_size + 17`. Assert
  byte-identity and that the reported hash matches.
- Chunk boundary: assert the last request carries `chunk: -1` and that a
  single-chunk upload carries no `chunk` key at all.
- `chunk_size > MAX_SAFE_CHUNK_SIZE` is rejected before any request is made.
- Resume: truncate a remote file to a chunk multiple, resume, assert
  byte-identity; truncate to a non-multiple, assert the restart-from-scratch
  path is taken and that it is reported.
- Conflict: existing destination without `overwrite` raises `TransferConflict`
  with `reason="exists"`; stale `expect_hash` raises with `reason="stale"`.
- Transport fallback: a stubbed `/files/` returning 200 (Range ignored) and one
  returning 404 both produce byte-identical output via their fallbacks.
- Interrupted download leaves no file at `local_path`, only a `.part`.
- `kernel_contents_dir` returns `contents_dir: null`, not a guess, for a cwd
  outside `root_dir`; and resolves correctly when two candidate suffixes both
  exist (the case the sentinel exists to disambiguate).
- Progress: monotonically non-decreasing `bytes_done`, final event equals
  `bytes_total`, rate limit respected.

Prefer a fake `requests.Session` over a live server for the protocol-shape
tests; keep one opt-in integration test against a real `jupyter server`.

## 10. Out of scope for v1

- Transfer through the kernel (§4.1). The seam exists; the code does not.
- Directory / recursive transfer. Single files only. `make_directory` exists so
  a destination path can be created, not so trees can be walked.
- Sync or watch semantics. Every transfer is explicit and one-shot.
- Checkpoints, and the `/api/contents/<path>/checkpoints` endpoints.
