# jsonyter

A JSON-first Python interface to a [Jupyter server](https://jupyter-server.readthedocs.io/).
Every call makes web requests to a local or remote Jupyter server and returns
plain Python objects (dicts/lists) that serialize directly with `json.dumps` —
no custom classes in the results, no notebook machinery.

The intended use case is powering a functional REPL from an editor that speaks
JSON, Emacs in particular: the bundled `jsonyter` command exposes the whole
library as a line-oriented JSON protocol over stdin/stdout, so Emacs can run it
with `make-process` and parse replies with `json-parse-string`.

## Install

```bash
pip install -e .
```

Dependencies: `requests` (REST API), `websocket-client` (kernel channels) and
`nbformat` (local `.ipynb` read/write). You also need a Jupyter server to talk
to, e.g. `pip install jupyter-server ipykernel` then
`jupyter server --ServerApp.token=SECRET` — though the notebook file methods
work without one.

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
| Contents | `get_contents` |
| Notebooks (local files) | `read_notebook`, `write_notebook`, `notebook_hash` |
| Kernel (WebSocket) | `execute`, `complete`, `inspect`, `is_complete`, `kernel_info`, `history` |
| Events | `add_listener`/`remove_listener` (library), `subscribe`/`unsubscribe` (bridge) |

The kernel channel speaks the [Jupyter messaging protocol](https://jupyter-client.readthedocs.io/en/stable/messaging.html)
v5.3; message construction lives in `jsonyter/messages.py` if you need a
message type that isn't wrapped yet.
