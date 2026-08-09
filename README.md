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

Dependencies: `requests` (REST API) and `websocket-client` (kernel channels).
You also need a Jupyter server to talk to, e.g. `pip install jupyter-server ipykernel`
then `jupyter server --ServerApp.token=SECRET`.

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

### Timeouts

`Client` takes two independent timeouts:

- `timeout` (default `10.0`s) bounds REST calls (`status`, `start_kernel`, ...)
  and the initial WebSocket handshake. Keep this short so a dead/unreachable
  server fails fast.
- `exec_timeout` (default `None`) is the default wait for a kernel reply on
  `execute`/`complete`/`inspect`/`is_complete`/`kernel_info`/`history`,
  measured as *silence since the last message* — receiving any message,
  including intermediate stream output, resets the clock, so it isn't a cap
  on total run time. It defaults to waiting indefinitely, since a REPL
  shouldn't impose an arbitrary deadline on someone's code, and some kernels
  (e.g. SAS) can take a long time just to become responsive on a fresh
  connection.

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

## The JSON stdio bridge (for Emacs)

```bash
jsonyter --url http://localhost:8888 --token SECRET
# slow kernel (e.g. SAS): give execute/etc a generous default, or omit
# --exec-timeout entirely to wait indefinitely (the default)
jsonyter --url https://jupyter.example.com --token SECRET --exec-timeout 120
```

One JSON request per line in, one JSON response per line out:

```json
{"id": 1, "method": "start_kernel", "params": {"name": "python3"}}
{"id": 1, "result": {"id": "8fca6bcb-...", "name": "python3", "execution_state": "starting"}}

{"id": 2, "method": "execute", "params": {"kernel_id": "8fca6bcb-...", "code": "1 + 1"}}
{"id": 2, "result": {"status": "ok", "execution_count": 1, "outputs": [{"type": "execute_result", "data": {"text/plain": "2"}, "metadata": {}, "execution_count": 1}]}}
```

If executed code calls `input()`, the bridge emits
`{"id": 2, "input_request": {"prompt": "? ", "password": false}}` and waits for
a `{"input": "the answer"}` line before the final result. Pass `--pretty` to
indent responses when driving the bridge by hand (editors should not use it —
it breaks the one-line-per-response framing). Errors come back as
`{"id": N, "error": {...}}` and never kill the process. Send
`{"id": 0, "method": "methods"}` to list every available method.

## Emacs sketch

A minimal comint-free REPL loop — enough to show the shape of the integration:

```elisp
(defvar jsonyter--proc nil)
(defvar jsonyter--kernel-id nil)
(defvar jsonyter--callbacks (make-hash-table :test #'eql))
(defvar jsonyter--next-id 0)
(defvar jsonyter--buffer "")

(defun jsonyter-start (url token)
  (setq jsonyter--proc
        (make-process
         :name "jsonyter"
         :command (list "jsonyter" "--url" url "--token" token)
         :connection-type 'pipe
         :filter #'jsonyter--filter))
  (jsonyter-request "start_kernel" '(:name "python3")
                    (lambda (reply)
                      (setq jsonyter--kernel-id
                            (plist-get (plist-get reply :result) :id)))))

(defun jsonyter-request (method params callback)
  (let ((id (cl-incf jsonyter--next-id)))
    (puthash id callback jsonyter--callbacks)
    (process-send-string
     jsonyter--proc
     (concat (json-serialize (list :id id :method method :params params))
             "\n"))))

(defun jsonyter--filter (_proc chunk)
  (setq jsonyter--buffer (concat jsonyter--buffer chunk))
  (while (string-match "\\(.*\\)\n" jsonyter--buffer)
    (let* ((line (match-string 1 jsonyter--buffer))
           (reply (json-parse-string line :object-type 'plist)))
      (setq jsonyter--buffer (substring jsonyter--buffer (match-end 0)))
      (when-let ((cb (gethash (plist-get reply :id) jsonyter--callbacks)))
        (unless (plist-get reply :input_request) ; final reply -> pop callback
          (remhash (plist-get reply :id) jsonyter--callbacks))
        (funcall cb reply)))))

(defun jsonyter-eval (code)
  (interactive "sPython: ")
  (jsonyter-request
   "execute" (list :kernel_id jsonyter--kernel-id :code code)
   (lambda (reply)
     (dolist (output (append (plist-get (plist-get reply :result) :outputs) nil))
       (pcase (plist-get output :type)
         ("stream" (message "%s" (plist-get output :text)))
         ("execute_result"
          (message "=> %s" (plist-get (plist-get output :data) :text/plain)))
         ("error" (message "%s" (plist-get output :evalue))))))))
```

A real mode would render `is_complete` on RET, feed `complete` into
`completion-at-point`, show `inspect` in eldoc, and decode `image/png`
mimebundles into inline images — all of which are single `jsonyter-request`
calls with the plumbing above.

## API surface

| Area | Methods |
| --- | --- |
| Server | `status`, `version` |
| Kernels (REST) | `list_kernelspecs`, `list_kernels`, `start_kernel`, `get_kernel`, `shutdown_kernel`, `restart_kernel`, `interrupt_kernel` |
| Sessions | `list_sessions`, `create_session`, `get_session`, `delete_session` |
| Contents | `get_contents` |
| Kernel (WebSocket) | `execute`, `complete`, `inspect`, `is_complete`, `kernel_info`, `history` |

The kernel channel speaks the [Jupyter messaging protocol](https://jupyter-client.readthedocs.io/en/stable/messaging.html)
v5.3; message construction lives in `jsonyter/messages.py` if you need a
message type that isn't wrapped yet.
