# jsonyter

[![PyPI version](https://img.shields.io/pypi/v/jsonyter.svg)](https://pypi.org/project/jsonyter/)
[![Downloads](https://img.shields.io/pypi/dm/jsonyter.svg)](https://pypistats.org/packages/jsonyter)
[![Python versions](https://img.shields.io/pypi/pyversions/jsonyter.svg)](https://pypi.org/project/jsonyter/)
[![Build and publish](https://github.com/EGuthrieWasTaken/jsonyter/actions/workflows/publish.yml/badge.svg)](https://github.com/EGuthrieWasTaken/jsonyter/actions/workflows/publish.yml)
[![License: GPL-3.0-only](https://img.shields.io/pypi/l/jsonyter.svg)](https://github.com/EGuthrieWasTaken/jsonyter/blob/main/LICENSE)

A JSON-first Python interface to a [Jupyter server](https://jupyter-server.readthedocs.io/).
Every call makes web requests to a local or remote Jupyter server and returns
plain Python objects (dicts/lists) that serialize directly with `json.dumps` —
no custom classes in the results, no notebook machinery.

The intended use case is powering a functional REPL from an editor that speaks
JSON, Emacs in particular: the bundled `jsonyter` command exposes the whole
library as a line-oriented JSON protocol over stdin/stdout, so Emacs can run it
with `make-process` and parse replies with `json-parse-string`.

> **A note on how this was built.** The bulk of jsonyter was written by
> Claude Fable 5, an Anthropic AI model, working iteratively with the
> project's maintainer over the course of development — design decisions,
> requirements and review were mine; the code, and much of the
> exploratory verification behind it, were largely the model's.

## Install

```bash
pip install jsonyter
# or
uv add jsonyter
```

Both install the latest release from PyPI; use whichever matches the rest of
your workflow.

To track the unreleased code in this repo instead — for testing a fix ahead
of a release, say — install from a checkout:

```bash
git clone https://github.com/EGuthrieWasTaken/jsonyter.git
cd jsonyter
pip install -e .
# or: uv pip install -e .
```

The tip of `main` isn't guaranteed to be functional; prefer a tagged release
unless you specifically need code that hasn't shipped yet.

Dependencies: `requests` (REST API), `websocket-client` (kernel channels) and
`nbformat` (local `.ipynb` read/write) — all installed automatically. You also
need a Jupyter server to talk to, e.g. `pip install jupyter-server ipykernel`
then `jupyter server --ServerApp.token=SECRET` — though the notebook file
methods work without one.

## Library usage

```python
from jsonyter import Client

client = Client("http://localhost:8888", token="SECRET")

client.status()            # {"version": "2.x", "kernels": 0, ...}
client.list_kernelspecs()  # {"default": "python3", "kernelspecs": {...}}

kernel = client.start_kernel("python3")   # {"id": "...", "name": "python3", ...}

with client.kernel(kernel["id"]) as conn:
    conn.execute("x = 40 + 2")
    result = conn.execute("print('hi'); x")
    # {"status": "ok",
    #  "execution_count": 2,
    #  "outputs": [
    #    {"type": "stream", "name": "stdout", "text": "hi\n"},
    #    {"type": "execute_result", "data": {"text/plain": "42"},
    #     "metadata": {}, "execution_count": 2}]}

    conn.complete("impor")        # {"matches": ["import", ...], ...}
    conn.inspect("print")         # {"found": true, "data": {...}, ...}
    conn.is_complete("for i in:") # {"status": "invalid"} — drives Enter vs newline
    conn.execute("input('? ')", stdin_callback=lambda req: "answer")

client.shutdown_kernel(kernel["id"])
```

Everything is JSON-renderable, including errors:

```python
from jsonyter import JupyterError
try:
    client.get_kernel("nope")
except JupyterError as err:
    err.to_json()  # {"error": "JupyterError", "message": "...", "status": 404, "url": "..."}
```

Every public method also takes `pretty` (default `False`); with
`pretty=True` it returns an indented JSON string instead of the Python object,
handy at an interactive prompt:

```python
print(client.status(pretty=True))
# {
#   "connections": 0,
#   "kernels": 0,
#   ...
# }
```

Rich output arrives as Jupyter mimebundles (`{"text/plain": ..., "image/png":
base64, "text/html": ...}`) inside `display_data`/`execute_result` outputs;
the front end picks the representation it can render.

### Streaming output

`execute` blocks until the cell finishes, but `on_output` fires as each output
arrives, so a long-running cell can render `print` output while it runs
instead of dumping it at the end:

```python
conn.execute(slow_code, on_output=lambda out: render(out))
```

The same dicts still appear in the returned `outputs`, so the callback is
purely additive.

### Async kernel events

A background thread pumps the kernel socket, so kernel state is observable
without polling `get_kernel` and without an `execute` in flight:

```python
conn.add_listener(lambda ev: print(ev))
# {"type": "status", "execution_state": "busy", "kernel_id": "..."}
# {"type": "status", "execution_state": "idle", "kernel_id": "..."}
# {"type": "dead", "kernel_id": "...", "restart": false}   <- kernel shut down
# {"type": "disconnected", "message": "..."}               <- socket dropped
conn.execution_state   # last seen state, "dead", or None
```

A server often keeps the socket open for a moment after a kernel goes away, so
`dead` (from the kernel's `shutdown_reply`) is the timely death signal;
`disconnected` follows whenever the socket itself drops.

`dead` is sticky. A dying kernel emits one last `status: idle` *after* its
`shutdown_reply`, which would otherwise flip a naive state tracker back to
"idle"; those trailing `status` events are suppressed and `execution_state`
stays `"dead"`, so consumers don't each have to rediscover the ordering trap.

Because the pump owns the socket, another thread may call
`client.interrupt_kernel(kernel_id)` while `execute` is blocked — that's the
supported way to stop a runaway cell. Listener callbacks run on the pump
thread and must not block.

### Timeouts

`Client` takes three independent timeouts:

- `timeout` (default `10.0`s) bounds REST calls (`status`, `start_kernel`, ...)
  and the initial WebSocket handshake. Keep this short so a dead/unreachable
  server fails fast.
- `exec_timeout` (default `None`) is the default wait for a kernel reply on
  `execute`, measured as *silence since the last message* — receiving any
  message, including intermediate stream output, resets the clock, so it
  isn't a cap on total run time. It defaults to waiting indefinitely, since a
  REPL shouldn't impose an arbitrary deadline on someone's code, and some
  kernels (e.g. SAS) can take a long time just to become responsive on a
  fresh connection.
- `control_timeout` (default `30.0`s) is the same deadline for the
  introspection calls — `complete`, `inspect`, `is_complete`, `kernel_info`,
  `history`. These are bounded, interactive-latency operations, so unlike
  `execute` they are **not** allowed to wait forever by default: kernels
  exist that never answer some of them at all (the SAS kernel never replies
  to `history_request`), and an unbounded wait there wedges the connection
  permanently. Pass `None` to opt into waiting indefinitely anyway.
- `export_timeout` (default `120.0`s) bounds a single `export_notebook`
  request. A real notebook's PDF render is seconds-to-minutes, while
  `timeout` exists to make a dead server fail fast — export gets its own,
  much larger, deadline.

```python
client = Client("https://jupyter.example.com", token="...", exec_timeout=120)
```

Every kernel method also takes a per-call `timeout=` that overrides the
client default for just that call:

```python
conn.execute(sas_code, timeout=300)   # this call only
conn.execute(quick_code)              # falls back to client.exec_timeout
```

If a kernel is genuinely stuck rather than just slow, reclaim it with
`client.interrupt_kernel(kernel_id)` or `client.restart_kernel(kernel_id)`
instead of guessing a timeout.

### `is_complete` and trailing newlines

Pass code to `is_complete` as it would be *submitted* — newline-terminated —
rather than as raw buffer text. Kernels disagree about trailing newlines and
several read their absence as "more input coming": the SAS kernel calls
anything unterminated `incomplete` (even `""`), and CPython reports
`"def f():\n    return 1"` incomplete bare but complete once terminated.
Genuinely unfinished input still reports `incomplete` either way (verified on
python3, ir, julia and sas). The library deliberately doesn't append the
newline for you — it's a thin protocol wrapper, and rewriting user code is the
front end's call.

## Local notebook files

Reading and writing `.ipynb` files are plain filesystem operations — **no
server and no kernel are needed**, so an offline editor can still save. They
exist because serialization has to happen on the Python side: `nbformat`
round-trips a notebook byte-identically, while a naive JSON re-encode
collapses Jupyter's indentation and turns every save into a whole-file diff.

```python
client.write_notebook("/path/nb.ipynb", [
    {"id": "a1b2c3", "cell_type": "code",     "source": "print(1)"},
    {"id": "d4e5f6", "cell_type": "markdown", "source": "# heading"},
    {"id": None,     "cell_type": "code",     "source": "new cell"},
])
# {"path": "...", "cells": ["a1b2c3", "d4e5f6", "9z8y7x"],
#  "written": True, "hash": "sha256..."}
```

The client sends cell **source** (and, opt-in, outputs — see below).
`write_notebook` is a read-modify-write against the file on disk and rebuilds
the cell list in the order given:

- an `id` matching an existing cell **reuses that cell**, replacing only
  `source` — its `outputs`, `execution_count`, `metadata` and `attachments`
  survive, so reordering and editing preserve results;
- `id: null` or an unknown id creates a fresh cell;
- an existing cell not in the list is deleted;
- a changed `cell_type` drops that cell's `outputs` and `execution_count`
  (and `attachments` when becoming code, which can't carry them).

Notebook-level `metadata`, `nbformat` and `nbformat_minor` are preserved.
Notebooks older than nbformat 4.5 have no cell ids, so cells are matched by
position instead and no ids are written back.

**Outputs are not written by default.** Execution results are session-only, so
an ordinary save stays diff-sized and figure-free; stored outputs in the file
are preserved but never updated. An `outputs` key on a cell spec is ignored
entirely unless you opt in.

### Persisting outputs (`include_outputs`)

Pass `include_outputs=True` to persist freshly generated results for that save
only:

```python
client.write_notebook(path, [
    {"id": "a1b2c3", "cell_type": "code", "source": "print(1)",
     "execution_count": 3,
     "outputs": [{"output_type": "stream", "name": "stdout", "text": "1\n"}]},
    {"id": "d4e5f6", "cell_type": "markdown", "source": "# heading"},
    {"id": "9z8y7x", "cell_type": "code", "source": "unrun cell"},
], include_outputs=True)
```

- A spec carrying an `outputs` key (**even `[]`**) *replaces* that cell's
  stored outputs and `execution_count` — a fresh run replaces prior output
  rather than appending, matching Jupyter's own semantics. Above, `a1b2c3`
  gets new outputs.
- A spec **omitting** `outputs` leaves the stored ones untouched, so a client
  can send only the cells it actually re-ran. Above, `9z8y7x` keeps whatever
  is already on disk.
- Non-code cells never receive outputs, whatever the flag says.

Outputs use the four nbformat types — `stream` (`name`, `text`),
`display_data`/`execute_result` (`data`, `metadata`, plus `execution_count`
for `execute_result`), and `error` (`ename`, `evalue`, `traceback`) — and are
rebuilt through `nbformat.v4.new_output`, so each is validated individually.
A malformed one raises `JupyterError` before anything is written, exactly like
an invalid `cell_type`.

Writes go to a temp file in the same directory and are moved into place with
`os.replace`, so an interrupted save can never truncate the original, and the
notebook is validated before any of that happens. Pass `expect_hash` (the
sha256 the client last saw, also returned by `notebook_hash`) to guard against
clobbering an external edit:

```python
client.write_notebook(path, cells, expect_hash=last_seen)
# raises NotebookConflict if the file changed; nothing is written
```

`read_notebook(path)` returns the notebook as normalized nbformat v4 with an
id guaranteed on every cell — its job is older notebooks (nbformat 3, or
4.0–4.4 without ids), since the merge above depends on ids existing. The file
itself is not modified. A `write_notebook` to a path that doesn't exist yet
creates the notebook.

## Exporting notebooks

`list_export_formats()` and `export_notebook()` wrap the Jupyter server's
nbconvert endpoints — jsonyter never runs nbconvert itself, so every format
the target server offers is supported, including third-party exporters
registered by entry point:

```python
client.list_export_formats()
# {"available": true,
#  "formats": {"html": {"output_mimetype": "text/html"}, "markdown": {...}, ...},
#  "reason": null}
```

`available: false` (with a `reason`) means the server doesn't serve
nbconvert at all — nbconvert isn't installed, or a downstream server app has
disabled the endpoints — rather than raising, since this is meant as a
capability probe.

Export has two modes, picked by which of `server_path`/`cells`/`notebook`
you pass (exactly one is required):

```python
# GET: export a notebook already saved server-side, as it is stored on disk
client.export_notebook("html", server_path="analysis.ipynb")

# POST: export in-memory buffer state — no file needs to exist on the server
client.export_notebook("markdown", cells=[
    {"cell_type": "code", "source": "print('hi')"},
])

# POST: export an existing nbformat dict (read_notebook, or a raw .ipynb load)
nb = client.read_notebook("analysis.ipynb")
client.export_notebook("markdown", notebook=nb)
```

Use `server_path` for a file already on the server; use `cells`/`notebook`
for a REPL front end's unsaved buffer, or a remote server the client can't
write to.

**This is where `write_notebook`'s outputs-off default bites.** Export
renders only the outputs already present in the notebook it's handed — a GET
export of a file saved through `write_notebook`'s default has *no results in
it*, since outputs are session-only and never written unless you pass
`include_outputs=True` to `write_notebook`. To export "what I just ran"
without changing your save semantics, use `cells=`/`notebook=` (POST mode),
where `export_notebook`'s own `include_outputs` defaults to `True` —
deliberately the opposite of `write_notebook`, since an export with no
results in it is nearly useless:

```python
# POST mode defaults to including outputs already on the cells
client.export_notebook("html", cells=[
    {"cell_type": "code", "source": "1 + 1",
     "execution_count": 1,
     "outputs": [{"output_type": "execute_result",
                  "data": {"text/plain": "2"}, "metadata": {},
                  "execution_count": 1}]},
])
client.export_notebook("html", cells=[...], include_outputs=False)  # opt out
```

`include_outputs` only applies to `cells`/`notebook`; `server_path` always
exports the file exactly as stored. `sanitize_html` is GET-only (it's a
server option `server_path` alone can use).

**A format's mimetype can lie.** `webpdf`, `qtpdf` and `qtpng` are reported
as `text/html` by the server (inherited from the HTML exporter they build
on) even though they emit PDF/PNG bytes; `export_notebook` already knows
this and encodes them as base64 regardless of the advertised type.

**Some exports come back as a bundle** — markdown with an image output, for
example, comes back as a zip of the document plus sidecar files. jsonyter
always unpacks it; you never receive a zip:

```python
client.export_notebook("markdown", server_path="withimage.ipynb")
# {"format": "markdown", "mimetype": "text/markdown", "extension": ".md",
#  "encoding": "text", "content": "...![png](output_0_0.png)\n",
#  "bundle": true,
#  "resources": [{"name": "output_0_0.png", "mimetype": "image/png",
#                 "encoding": "base64", "content": "iVBORw0KG..."}]}
```

`resources` entries are keyed by the basename the primary document already
references them by, so writing them alongside it (see `to_path` below) makes
the document work as-is. A non-bundle response looks the same shape minus
the resources:

```python
client.export_notebook("pdf", server_path="analysis.ipynb")
# {"format": "pdf", "mimetype": "application/pdf", "extension": ".pdf",
#  "encoding": "base64", "content": "JVBERi0xLjUK...", "bundle": false,
#  "resources": []}
```

`encoding` is `"text"` or `"base64"` depending on whether the bytes decode as
UTF-8 (binary formats are always base64, regardless).

Pass `to_path` to write the result to disk instead of returning it inline —
resources are always written alongside the primary file, under their own
basenames, overwriting same-named files:

```python
client.export_notebook("markdown", server_path="withimage.ipynb",
                       to_path="/tmp/out/analysis.md")
# {"format": "markdown", "mimetype": "text/markdown", "extension": ".md",
#  "path": "/tmp/out/analysis.md", "bytes": 51, "sha256": "...",
#  "bundle": true,
#  "resources": [{"name": "output_0_0.png", "path": "/tmp/out/output_0_0.png",
#                 "bytes": 74, "sha256": "..."}]}
```

If `to_path` names an existing directory (or ends in a path separator), the
filename is derived from `name`/`server_path` instead. Writes go to a temp
file in the destination directory and are moved into place with
`os.replace`, same as `write_notebook`, so an interrupted export can't
truncate an existing file.

A failed export raises `ExportError` (a `JupyterError`) with the server's
own message recovered from its HTML error page — never the page itself:

```python
from jsonyter import ExportError
try:
    client.export_notebook("pdf", server_path="analysis.ipynb")
except ExportError as err:
    err.message             # "Pandoc wasn't found. ..."
    err.hint                 # "the 'pdf' exporter needs pandoc and a LaTeX ..."
    err.available_formats    # populated only for "unknown export format" errors
```

See the [server-side toolchain requirements](#server-side-toolchain-for-pdf-exports)
below if `pdf`/`latex`/`webpdf` fail — those exporters need packages
installed on the **Jupyter server**, not in jsonyter.

### Server-side toolchain for PDF exports

nbconvert runs server-side, so `pdf`/`latex`/`webpdf` need packages on the
**Jupyter server's** image, not in jsonyter:

```dockerfile
# pdf / latex: pandoc + a LaTeX engine
RUN apt-get update && apt-get install -y --no-install-recommends \
        pandoc texlive-xetex texlive-fonts-recommended texlive-plain-generic \
    && rm -rf /var/lib/apt/lists/*
# add `inkscape` too if notebooks produce SVG outputs

# webpdf: headless Chromium via playwright, no LaTeX needed
RUN pip install --no-cache-dir "nbconvert[webpdf]" \
    && playwright install --with-deps chromium
```

Without pandoc, `pdf`/`latex` fail with `Pandoc wasn't found` (`export_notebook`
raises this as `ExportError.message`, with a `hint` naming the fix). CJK and
emoji glyphs render as tofu with the package set above — the LaTeX engine
also needs to be told to use a CJK-capable font (`fonts-noto-cjk` alone isn't
enough); treat that as a separate font-configuration task.

For `webpdf` in a container running as root, Chromium's sandbox needs to be
disabled server-side (`c.WebPDFExporter.disable_sandbox = True` in
`jupyter_server_config.py`); a browser launch failure of any kind — sandbox
refusal included — is reported by nbconvert as the same misleading "no
suitable chromium executable found" message, so if that appears after
`playwright install chromium` has already run, check for a
playwright/Chromium version mismatch before chasing anything else.

## Transferring files

`jsonyter` already straddles both filesystems — it runs as a subprocess of the
editor (ordinary local disk) and it is authenticated against the Jupyter
server's Contents API (the server's disk) — so a data file can move between
them without dropping to `scp`/`rclone`/the Lab UI. The chunking, hashing and
file I/O happen in Python; only progress counters cross the pipe.

```python
import jsonyter

client = jsonyter.Client("https://jupyter.example.com", token="SECRET")

up = jsonyter.upload(client, "/home/e/trials.csv", "data/trials.csv")
# {"path": "data/trials.csv", "local_path": "/home/e/trials.csv",
#  "bytes": 193273528, "chunks": 24, "hash": "3f2a…", "hash_algorithm": "sha256",
#  "verified": "sha256", "resumed_at": 0, "resume_note": null, "elapsed": 41.2}

jsonyter.download(client, "data/trials.csv", "/home/e/trials-copy.csv")
# same shape, plus "transport": "files" | "contents"
```

**Uploads are chunked, and that is not optional.** A gateway such as
Cloudflare caps the request body at 100 MB by default (200 MB Business, up to
5 GB Enterprise); a base64 body is 4/3 the raw size, so the safe ceiling is
~74 MB. The default chunk is **8 MiB** — well below the ceiling so memory,
progress granularity and the retry unit all stay small. Override it per call
with `chunk_size=` or bridge-wide with `--chunk-size`; anything over the safe
maximum is refused before a byte is sent.

**Downloads use a different route.** A whole-file `GET /api/contents`
base64-encodes and JSON-serialises the entire file before the first byte
leaves the server, which can trip a proxy's time-to-first-byte limit, so
`download` streams raw bytes from `/files/<path>` with HTTP `Range` instead —
no base64 tax, no timeout, and resumable. When `/files/` will not serve a
`206` (a non-file-backed contents manager — S3 and friends), it falls back to
the Contents API with `format=base64`; `transport` in the result says which
path ran.

**Both directions verify with a hash.** `GET /api/contents/<path>?content=0&hash=1`
(jupyter_server ≥ 2.11) returns a sha256 without transferring the file, and it
is compared against the local digest. An older server has no such hash, so the
check degrades to file size and the result says `"verified": "size"` instead
of `"sha256"`; a verification request that times out reports
`"verified": "unverified"` rather than failing the transfer.

**Resume is an optimisation, never the correctness argument.** `resume=True`
continues a partial upload when the server-side size is a whole number of
chunks (it appends blindly, so a partial chunk means restart-from-scratch —
`resume_note` says why); downloads resume against a local `.part` file, which
is `os.replace`d onto the real name only once the hash checks out, so an
interrupted download never leaves a truncated file at the real path.

**Conflicts reuse the `NotebookConflict` pattern.** `TransferConflict` (a
`JupyterError`) carries `path`/`expected_hash`/`actual_hash` plus a `reason`
so the front end can offer the right recovery:

```python
from jsonyter import TransferConflict
try:
    jsonyter.upload(client, "trials.csv", "data/trials.csv")
except TransferConflict as err:
    err.reason      # "exists"  -> pass overwrite=True (or resume=True)
                    # "stale"   -> expect_hash didn't match; download it first
                    # "corrupt" -> the bytes that landed are wrong; retry/resume
```

Pass `overwrite=True` to replace a destination, or `expect_hash=<sha256>` as a
staleness guard — a mismatch raises `reason="stale"` rather than clobbering a
file that moved under you.

A transfer that dies mid-flight fails with the numbers and the recovery
spelled out, and says whether the proxy or the Jupyter server refused:

```
upload of data/trials.csv failed at chunk 7/23 (12.4 MB of 184.0 MB written)
— Payload Too Large — HTTP 413 from the proxy (cf-ray 8a… present), not from
the Jupyter server — lower --chunk-size (currently 64.0 MB, ceiling ~74 MB),
then resume from byte 13008896
```

### The thin Contents API verbs

`upload`/`download` are built on plain wrappers over the Jupyter Contents API,
exposed on `Client` (and the bridge) in their own right:

| Method | Endpoint |
| --- | --- |
| `get_contents(path, content, type, format, hash)` | `GET /api/contents/<path>` |
| `put_contents(path, content, type, format, chunk)` | `PUT /api/contents/<path>` |
| `make_directory(path)` | `PUT` with `{"type": "directory"}` (names it exactly) |
| `delete_contents(path)` | `DELETE /api/contents/<path>` |
| `rename_contents(path, new_path)` | `PATCH` — old path in URL, new in body; move == rename |
| `copy_contents(path, to_dir)` | `POST` with `copy_from` — server-side, no bytes on the wire |
| `list_contents(path)` | `get_contents` on a directory |

### Kernel working directory vs. Contents paths

Contents paths are POSIX-style, relative to the server's `root_dir`, with no
leading slash. The kernel's working directory is a *different coordinate
system* and no API reports `root_dir`, so `kernel_contents_dir` learns the
mapping once per kernel: it asks the kernel for its cwd, has it drop an empty
`.jsonyter-probe-<uuid>` there, then walks the cwd's path suffixes
longest-first and takes the first Contents listing that actually contains the
sentinel.

```python
jsonyter.kernel_contents_dir(client, kernel_id)
# {"kernel_id": "...", "cwd": "/home/jovyan/work/analysis",
#  "contents_dir": "work/analysis", "root_dir": "/home/jovyan",
#  "method": "probe", "language": "python"}
```

`method` is `"probe"`, `"configured"` (you passed `root=` and the probe was
skipped), `"unresolved"` (no suffix resolved — the kernel is outside
`root_dir`; `contents_dir` is `null`, never a guess) or `"unsupported"` (no
snippet for the kernel's language — Python, R and Julia are covered; anything
else is not an error). On the bridge the result is cached per kernel and
dropped on restart/shutdown.

## The JSON stdio bridge (for Emacs)

```bash
JUPYTER_TOKEN=SECRET jsonyter --url http://localhost:8888
# slow kernel (e.g. SAS): give execute/etc a generous default, or omit
# --exec-timeout entirely to wait indefinitely (the default)
JUPYTER_TOKEN=SECRET jsonyter --url https://jupyter.example.com --exec-timeout 120
```

One JSON request per line in, one JSON response per line out:

```json
{"id": 1, "method": "start_kernel", "params": {"name": "python3"}}
{"id": 1, "result": {"id": "8fca6bcb-...", "name": "python3", "execution_state": "starting"}}

{"id": 2, "method": "execute", "params": {"kernel_id": "8fca6bcb-...", "code": "1 + 1"}}
{"id": 2, "result": {"status": "ok", "execution_count": 1, "outputs": [{"type": "execute_result", "data": {"text/plain": "2"}, "metadata": {}, "execution_count": 1}]}}
```

Errors come back as `{"id": N, "error": {...}}` and never kill the process.
Send `{"id": 0, "method": "methods"}` to list every available method. Pass
`--pretty` to indent responses when driving the bridge by hand (editors should
not use it — it breaks the one-line-per-response framing).

### Line types

Every line is a JSON object. Dispatch on which key is present — anything that
is not `result`/`error` is out-of-band and does not complete the request:

| Key | Meaning |
| --- | --- |
| `result` | final success response for `id` |
| `error` | final failure response for `id` |
| `output` | incremental output from a running `execute` |
| `input_request` | the kernel wants stdin; reply before it can finish |
| `event` | async kernel state, after `subscribe` |
| `progress` | transfer progress from a running `upload`/`download` (rate-limited to ~4/s; the last one has `bytes_done == bytes_total`) |

### Concurrency

Requests are handled concurrently: REST calls run on a small pool and each
kernel gets its own worker, so a blocked `execute` never stops the bridge from
reading stdin. You can send `interrupt_kernel` down the same pipe while code
is running and it is acted on immediately — no second "control" process
needed. **Responses may therefore arrive out of request order**; match them by
`id`.

```json
{"id": 2, "method": "execute", "params": {"kernel_id": "8fca...", "code": "while True: pass"}}
{"id": 3, "method": "interrupt_kernel", "params": {"kernel_id": "8fca..."}}
{"id": 3, "result": {"id": "8fca...", "interrupted": true}}
{"id": 2, "result": {"status": "error", "outputs": [{"type": "error", "ename": "KeyboardInterrupt", ...}]}}
```

### Streaming and events

Add `"stream": true` to an `execute` (or start the bridge with `--stream` to
make it the default) to get output as it is produced:

```json
{"id": 2, "method": "execute", "params": {"kernel_id": "8fca...", "code": "print('a'); print('b')", "stream": true}}
{"id": 2, "output": {"type": "stream", "name": "stdout", "text": "a\n"}}
{"id": 2, "output": {"type": "stream", "name": "stdout", "text": "b\n"}}
{"id": 2, "result": {"status": "ok", "execution_count": 1, "outputs": [ ...same two outputs... ]}}
```

`subscribe` reports kernel state transitions as they happen, so a front end
can show busy/idle (or notice a dead kernel) without polling:

```json
{"id": 4, "method": "subscribe", "params": {"kernel_id": "8fca..."}}
{"id": 4, "result": {"kernel_id": "8fca...", "subscribed": true, "execution_state": "idle"}}
{"kernel_id": "8fca...", "event": {"type": "status", "execution_state": "busy"}}
{"kernel_id": "8fca...", "event": {"type": "status", "execution_state": "idle"}}
{"kernel_id": "8fca...", "event": {"type": "dead", "restart": false}}
{"kernel_id": "8fca...", "event": {"type": "disconnected", "message": "..."}}
```

### stdin

If executed code calls `input()`, the bridge emits
`{"id": 2, "input_request": {"prompt": "? ", "password": false}}` and waits for
`{"id": 2, "input": "the answer"}` before the final result. A bare
`{"input": "..."}` still works when only one request is waiting.

### Tokens

`--token` is still accepted but puts the secret in the process's argv, where
any local user can read it with `ps` — which defeats a gpg-encrypted token
file. Prefer either:

```bash
JUPYTER_TOKEN=$(gpg -qd ~/.jupyter-token.gpg) jsonyter --url ...   # env
gpg -qd ~/.jupyter-token.gpg | jsonyter --token-file - --url ...   # first stdin line
jsonyter --token-file ~/.jupyter/token --url ...                   # a file
```

An editor spawning the bridge should set `JUPYTER_TOKEN` in the subprocess
environment rather than passing `--token` on the command line. The `Client`
class reads `JUPYTER_TOKEN` too, so library code never needs a hardcoded token
either.

## API surface

| Area | Methods |
| --- | --- |
| Server | `status`, `version` |
| Kernels (REST) | `list_kernelspecs`, `list_kernels`, `start_kernel`, `get_kernel`, `shutdown_kernel`, `restart_kernel`, `interrupt_kernel` |
| Sessions | `list_sessions`, `create_session`, `get_session`, `delete_session` |
| Contents | `get_contents`, `put_contents`, `make_directory`, `delete_contents`, `rename_contents`, `copy_contents`, `list_contents` |
| File transfer | `upload`, `download`, `kernel_contents_dir` (module functions / bridge methods, not `Client` methods) |
| Notebooks (local files) | `read_notebook`, `write_notebook`, `notebook_hash` |
| Export | `list_export_formats`, `export_notebook` |
| Kernel (WebSocket) | `execute`, `complete`, `inspect`, `is_complete`, `kernel_info`, `history` |
| Events | `add_listener`/`remove_listener` (library), `subscribe`/`unsubscribe` (bridge) |

The kernel channel speaks the [Jupyter messaging protocol](https://jupyter-client.readthedocs.io/en/stable/messaging.html)
v5.3; message construction lives in `jsonyter/messages.py` if you need a
message type that isn't wrapped yet.
