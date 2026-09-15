# Feature request: bidirectional directory sync between the client and the Jupyter server

**Status:** proposed
**Date:** 2026-09-15
**Scope:** `jsonyter` (this repo). A companion document in `jsonyter.el`
covers the Emacs side; the two are written to be implemented in that order,
because the elisp is purely a consumer of the protocol defined here.

**Supersedes:** the "sync or watch semantics" line in
`FEATURE-REQUEST-file-transfer.md` §10, which deferred exactly this.

---

## 1. Problem

`upload` and `download` moved the *single-file* case out of `scp`'s hands, and
`jsonyter-remote-dired` made the server's filesystem browsable. What is still
manual is the case those two compose into badly: **a directory of data files
that both ends touch.**

The concrete shape of it: a local `~/project/data/` holds inputs; the kernel
writes results next to them in the server's `work/data/`. Keeping the two in
agreement today means remembering which files changed, in which direction,
since the last time — and issuing an `upload` or a `download` for each. That
is bookkeeping a program should do, and the bookkeeping is exactly where a
human loses data: the file you forget to pull is the one you then overwrite.

The goal is one command — *make these two directories agree* — that is safe to
run repeatedly, moves only what actually differs, and never silently discards
an edit.

## 2. Scope, stated narrowly

**In:** an explicit, on-demand, bidirectional reconciliation of one local
directory against one Contents-API directory, driven by content hashes.

**Out:** continuous watching. There is no inotify, no polling loop, no daemon.
Every sync is a discrete call with a beginning and an end, because that is what
the client asked for and because it removes the hardest failure mode (a
background process quietly resolving conflicts while nobody is looking). §11
records what a future watch mode would need; do not build it now.

This is the same discipline the transfer work used: `transfer.py` is one-shot
and explicit, and this module inherits that.

## 3. Why this is not "upload everything, then download everything"

Three reasons, each of which shapes the design.

**Direction is not a property of a file, it is a property of a *change*.** A
naive pass that uploads the local tree and then downloads the remote tree does
not converge — it ping-pongs, and the second half undoes the first for every
file present on both sides.

**Two-way comparison cannot attribute a change.** Given only the current local
and remote states, "these differ" is all you can say. You cannot tell *which
side moved*, and so you cannot tell a one-sided edit (safe, automatic) from a
two-sided one (a genuine conflict, and the only case where data can be lost).
This is the argument for §5's baseline, and it is the single most important
decision in this document.

**Absence is ambiguous.** A file present in one tree and absent from the other
is either *newly created there* or *deleted here*. Those call for opposite
actions. Again, only a record of the previous agreement can tell them apart.

## 4. Hashes: sha256, not md5 — and read the algorithm, don't assume it

The request named md5 "or similar". Use **sha256**, for a reason that is about
bandwidth rather than cryptography:

`GET /api/contents/<path>?content=0&hash=1` returns a sha256 of a server-side
file **without transferring it** (jupyter_server ≥ 2.11; the server re-reads the
bytes to hash them). That is the entire basis on which a sync can be cheap.
There is no endpoint that will hash a remote file as md5, so choosing md5 would
mean *downloading every remote file in order to decide whether to download it* —
which inverts the point of the feature. `transfer.py` already computes and
compares sha256 in both directions, so this is also the choice that reuses what
exists.

Two caveats, both already handled elsewhere in this codebase and both of which
the sync layer must honour:

- **The algorithm is configurable.** `ContentsManager.hash_algorithm` defaults
  to sha256 but is a trait. Read `hash_algorithm` back from the model and hash
  the local side with *that* — never hardcode `hashlib.sha256` on the
  comparison path. A server configured for `sha1` must still sync correctly, or
  fail loudly; it must not silently compare a sha256 against a sha1 and declare
  every file different. `hashlib.new(model["hash_algorithm"])` with a guard for
  an unknown name.
- **Servers older than 2.11 return no `hash` key at all.** Degrade to
  `(size, mtime)` comparison, and mark the whole run `"integrity": "size"` so
  the caller can say out loud which guarantee it got — the same
  `verified: "sha256" | "size" | "unverified"` vocabulary `upload`/`download`
  already return. A size-only sync is a real degradation (a same-size edit is
  invisible to it) and must be reported as one, not hidden.

## 5. The core idea: three-way comparison against a baseline

Keep a **baseline** — a record of the (path, hash) set as of the last
successful sync of this pair. Every subsequent sync compares three states per
path: local (**L**), remote (**R**), and baseline (**B**).

This turns "they differ" into "*who* changed", which is the whole game:

| B | L | R | Condition | Action |
|---|---|---|---|---|
| – | ✓ | – | new local file | **push** |
| – | – | ✓ | new remote file | **pull** |
| – | ✓ | ✓ | `L == R` | **converge** — record baseline, move no bytes |
| – | ✓ | ✓ | `L ≠ R` | **conflict** (first sync: nothing to attribute the change to) |
| ✓ | ✓ | ✓ | `L == B`, `R == B` | **in-sync** — skip |
| ✓ | ✓ | ✓ | `L ≠ B`, `R == B` | **push** |
| ✓ | ✓ | ✓ | `L == B`, `R ≠ B` | **pull** |
| ✓ | ✓ | ✓ | both ≠ B, `L == R` | **converge** — both ends made the same edit |
| ✓ | ✓ | ✓ | both ≠ B, `L ≠ R` | **conflict** |
| ✓ | – | ✓ | `R == B` | deleted locally → **push-delete** |
| ✓ | – | ✓ | `R ≠ B` | **conflict**: deleted here, edited there |
| ✓ | ✓ | – | `L == B` | deleted remotely → **pull-delete** |
| ✓ | ✓ | – | `L ≠ B` | **conflict**: edited here, deleted there |
| ✓ | – | – | gone from both | **forget** — drop the baseline entry |

Read the table as the specification. Every branch of `_classify` should be
traceable to one row, and the test suite (§10) should have one case per row.

Two rows earn their place specifically:

- **converge** moves no bytes. Two ends that independently arrived at identical
  content are in agreement; transferring either onto the other is a no-op that
  costs a round trip and rewrites an mtime. Record and move on.
- **first-sync conflict.** With no baseline, a file that exists on both sides
  with different content is genuinely unattributable, and the honest answer is
  to say so rather than to guess. In practice the conflict policy (§6) resolves
  it immediately — but it resolves it *as a conflict*, visibly, which is the
  difference between "newest won" and "something was lost".

### 5.1 Where the baseline lives

A JSON file, one per synced pair, keyed by `(server URL, remote dir, local
dir)`:

```
$XDG_STATE_HOME/jsonyter/sync/<sha256(url + "\0" + remote + "\0" + local)[:16]>.json
```

falling back to `~/.local/state/jsonyter/sync/`. Expose `state_path` as an
explicit parameter so a caller can put it elsewhere.

```json
{
  "version": 1,
  "server": "https://jupyter.example.org",
  "local_dir": "/home/e/project/data",
  "remote_dir": "work/data",
  "synced_at": "2026-09-15T18:04:11Z",
  "hash_algorithm": "sha256",
  "entries": {
    "trials.csv": {
      "hash": "3f2a...",
      "size": 193273528,
      "local_mtime": 1757260000.123,
      "local_inode": 8419223,
      "remote_mtime": "2026-09-07T14:22:00Z"
    }
  }
}
```

**Not a dotfile inside the synced tree.** It would have to exclude itself from
its own sync, it would appear in the user's data directory, and two clients
syncing the same remote directory would fight over one file. The baseline is
*per-client state about an agreement*, not shared data — each client keeping
its own is correct, and two clients converge anyway because each reconciles
against the same remote.

Write it atomically (`tmp` + `os.replace`) and only after the transfers it
describes have completed. A crash mid-sync must leave either the old baseline
or a correct newer one, never a half-written file. Per-file: update an entry
only when *that file's* transfer verified, so a run that fails at file 7 of 12
still records the 6 that landed and the next run picks up where it stopped.

### 5.2 A missing or unreadable baseline is not an error

Treat it as a first sync: every shared path becomes `converge` or `conflict`,
and nothing is deleted (§7). A corrupt baseline is discarded with a warning in
the result, never a traceback. This matters because the baseline is disposable
by design — deleting it must always be a safe recovery action, not a way to
break the pair.

## 6. Conflicts

A conflict is the only case where a sync can destroy work, so it gets an
explicit policy rather than a default buried in the code.

```python
CONFLICT_POLICIES = ("ask", "newest", "local", "remote", "skip")
```

- **`ask`** — the default for `sync_plan`. The plan comes back with the entry
  marked `action: "conflict"` and both sides' metadata attached; the caller
  decides. This is what `jsonyter.el` uses, and it is why §8 splits plan from
  apply.
- **`newest`** — the requested behaviour: the side with the later modification
  time wins. The default for the one-shot `sync()` convenience path.
- **`local`** / **`remote`** — a fixed side wins.
- **`skip`** — leave both alone, report it, exit non-zero-ish (`conflicts` in
  the result is non-empty).

### 6.1 `newest` needs a clock, and there are two of them

`last_modified` comes from the server's clock; `os.stat().st_mtime` from the
client's. They are not the same clock, and on a remote Jupyter deployment they
can differ by minutes. "The most recent version wins" is therefore only as
trustworthy as the skew between them, and a naive comparison will confidently
pick the wrong file.

Three rules:

1. **Hashes decide *whether* something differs; the baseline decides *which
   side changed*; mtime is consulted only to break a conflict.** By the time a
   timestamp is compared, both sides are already known to have changed. This
   confines the damage of a bad clock to the case that was already ambiguous.
2. **Measure the skew.** Every `requests` response carries a `Date` header from
   the server. Sample it once per sync — no extra request needed, take it off
   the first listing — and record
   `skew = server_date - local_time_at_receipt`. Subtract it before comparing.
3. **Refuse to guess inside the noise.** If the two mtimes are within
   `max(2 × |skew|, SKEW_TOLERANCE)` of each other (default 5 s), `newest`
   cannot honestly pick, so it **degrades to `ask`** — the entry stays a
   conflict. A near-tie is exactly the case where an arbitrary choice is a
   silent data loss.

Report the skew in the result so a badly-set server clock is diagnosable rather
than merely mysterious:

```
clock skew vs the server is +47 s; 2 conflict(s) were too close to call and
were left unresolved
```

### 6.2 Never overwrite a conflict loser without a copy

When a policy resolves a conflict, the losing side's content is about to be
destroyed. Before the transfer, rename it aside:

```
trials.csv  ->  trials.csv.jsonyter-conflict-20260915T180411Z
```

Local losers are renamed on local disk; remote losers via `rename_contents`
(server-side, no bytes cross the wire). Controlled by `keep_conflict_copies`,
default **`True`**. Conflict copies are excluded from the next sync by the
default ignore list (§9), so they do not themselves become sync traffic.

This is cheap insurance and it is what makes `newest` acceptable as a default
at all: the wrong guess is recoverable.

## 7. Deletions are opt-in

Propagating a deletion is the most dangerous thing a sync can do, because the
evidence for "deleted" and the evidence for "not yet created" are identical
without a baseline, and because an accidental `rm -rf` on one side becomes an
accidental `rm -rf` on both.

```python
DELETE_POLICIES = ("none", "push", "pull", "both")
```

Default **`"none"`**: a file missing on one side is reported as
`local_missing` / `remote_missing` and left alone. The table's `push-delete` /
`pull-delete` rows only fire under `push` / `pull` / `both`.

Two hard guards, regardless of policy:

- **No baseline, no deletions.** Without B, the `–/✓` rows are unattributable;
  a first sync must never delete. Enforce this in `sync_plan`, not in the
  caller.
- **A bulk-deletion brake.** If deletions exceed `max_deletes` (default 25) or
  half the baseline's entries, abort the plan with a `SyncRefused` naming the
  count and the override. The case this catches is a local directory that
  vanished — an unmounted volume, a wrong `local_dir`, a checkout on a
  different branch — which otherwise reads as "the user deleted everything" and
  faithfully destroys the remote copy. An unmounted mountpoint looks exactly
  like an empty directory, and this is the guard that keeps that from being
  catastrophic.

Directories are never deleted, only files. An emptied directory is left in
place; removing it is not worth the failure modes.

## 8. API: `jsonyter/sync.py`

A new module, sibling to `transfer.py`, which it calls for every byte that
moves. Same discipline: no `cli.py` import, takes a `progress` callable, knows
nothing about the JSON protocol.

```python
DEFAULT_IGNORE = (
    ".git/", ".hg/", ".svn/", "__pycache__/", ".ipynb_checkpoints/",
    ".DS_Store", "*.pyc", "*.pyo", "*.swp", "*~", "*.part",
    "*.jsonyter-conflict-*",
)
SKEW_TOLERANCE = 5.0        # seconds; below this, `newest` will not guess
DEFAULT_MAX_DELETES = 25
DEFAULT_MAX_FILES = 5000    # a listing guard, not a transfer guard


class SyncRefused(JupyterError):
    """The plan was not safe to build or run; nothing was changed.

    ``reason`` is one of ``"too-many-deletes"``, ``"too-many-files"``,
    ``"too-large"``, ``"locked"``, ``"algorithm-mismatch"``. Carries the
    numbers and the name of the parameter that overrides the guard.
    """


def sync_plan(client, local_dir, remote_dir, *, ignore=None, follow_links=False,
              conflict="ask", delete="none", state_path=None,
              max_files=DEFAULT_MAX_FILES, max_deletes=DEFAULT_MAX_DELETES,
              progress=None):
    """Compare both trees against the baseline; move nothing.

    Returns the plan described in §8.1. Safe to call at any time: it is
    strictly read-only on both ends and on the baseline.
    """


def sync_apply(client, plan, *, overrides=None, chunk_size=None,
               keep_conflict_copies=True, progress=None, should_cancel=None):
    """Execute a plan from :func:`sync_plan`.

    ``overrides`` maps a relative path to a replacement action
    (``"push"`` / ``"pull"`` / ``"skip"`` / ``"push-delete"`` /
    ``"pull-delete"``), which is how an interactive front end resolves
    ``ask`` conflicts and countermands individual rows.

    ``should_cancel`` is polled between files (§8.4).
    """


def sync(client, local_dir, remote_dir, *, conflict="newest", **kwargs):
    """plan + apply in one call, for non-interactive use.

    Note the different ``conflict`` default: a caller that cannot answer a
    question should not be asked one.
    """
```

### 8.1 The plan

A plan is data — JSON-serialisable, inspectable, and the thing a UI renders.
It is also the unit of testing: §10's decision-table cases assert on plans,
with no I/O beyond the fakes.

```json
{
  "local_dir": "/home/e/project/data",
  "remote_dir": "work/data",
  "server": "https://jupyter.example.org",
  "hash_algorithm": "sha256",
  "integrity": "sha256",
  "baseline": "present",
  "clock_skew": 0.4,
  "scanned": {"local": 118, "remote": 121, "directories": 9, "requests": 14},
  "entries": [
    {"path": "trials.csv", "action": "push", "reason": "local-changed",
     "local":  {"size": 193273528, "hash": "3f2a...", "mtime": 1757260000.1},
     "remote": {"size": 193000000, "hash": "9c11...",
                "mtime": "2026-09-07T14:22:00Z"},
     "baseline_hash": "9c11...", "bytes": 193273528},
    {"path": "notes.md", "action": "conflict", "reason": "both-changed",
     "resolution": null, "newest": "remote", "mtime_delta": 1820.0, ...},
    {"path": "out/fig.png", "action": "pull", "reason": "remote-new", ...},
    {"path": "old.csv", "action": "skip", "reason": "remote-missing", ...}
  ],
  "totals": {"push": 3, "pull": 5, "converge": 1, "skip": 110,
             "conflict": 1, "push_delete": 0, "pull_delete": 0,
             "bytes_up": 193273528, "bytes_down": 4211},
  "warnings": ["clock skew vs the server is +47 s"]
}
```

`reason` is a stable machine token (`local-changed`, `remote-changed`,
`both-changed`, `local-new`, `remote-new`, `identical`, `local-missing`,
`remote-missing`, `unchanged`, `ignored`) mapping one-to-one onto §5's table.
The front end formats it; it must never have to parse prose.

`newest` and `mtime_delta` are precomputed on every conflict so an interactive
caller can show "the server's copy is 30 minutes newer" without redoing the
skew arithmetic.

### 8.2 Scanning cheaply — the request-count problem

Listings do **not** carry hashes: `list_contents` children arrive with
`name`/`path`/`type`/`size`/`last_modified`/`writable` and nothing else.
Hashing a remote file is therefore one `get_contents(..., content=False,
hash=True)` request *each*. A 500-file tree hashed naively is 500 round trips
per sync, which on a Cloudflare-fronted server is minutes of latency before a
single byte moves. This is the performance problem of the whole feature and it
has a clean answer.

**Use the baseline as a trust cache**, the way `git` and `rsync` do:

1. **Walk the remote tree** with `list_contents`, recursively: one request per
   directory. Free `size` + `last_modified` for every file.
2. **Walk the local tree** with `os.scandir`: free `size` + `st_mtime` +
   `st_ino`.
3. **A side is unchanged if its cheap metadata still matches the baseline's.**
   Locally that is `(size, mtime, inode)`; remotely `(size, last_modified)`.
   Take the baseline's recorded hash and issue **no request at all**.
4. **Hash only what fails that check**, and only on the side that failed it.
5. If both sides are known-unchanged, the file is `in-sync` and never hashed on
   either end.

Steady state therefore costs ~one request per directory plus a handful for
genuinely-changed files, and re-syncing an unchanged 500-file tree is ~9
requests, not 500. Report the count in `scanned.requests` so a regression here
is visible rather than merely slow.

The trust cache is an optimisation over a correctness argument, exactly like
`transfer.py`'s resume: a file edited within the mtime granularity *and* to the
same byte length would be missed, so expose `rehash=True` to force a full hash
of both trees, and have the post-transfer verification in `upload`/`download`
(which is unconditional) remain the actual guarantee.

`max_files` guards the walk itself: a `local_dir` accidentally pointed at `$HOME`
should fail fast with `SyncRefused(reason="too-many-files")`, before the
requests.

### 8.3 Executing

Iterate the plan in a fixed order — `pull` before `push`, alphabetical within
each — so a run is reproducible and a partial run is comprehensible.

Per entry:

1. Re-apply `overrides`. An overridden `conflict` becomes its resolved
   direction; anything overridden to `skip` is dropped.
2. Rename the conflict loser aside if §6.2 applies.
3. `make_directory` / `os.makedirs` the destination parent as needed. Cache
   which directories are known to exist so a 200-file tree does not re-issue
   the same `PUT` 200 times.
4. Call `transfer.upload` / `transfer.download` with `overwrite=True` and
   **`expect_hash` set to the hash the plan saw**. This is the guard against
   the scan→apply window: if the destination moved between planning and
   applying, `TransferConflict(reason="stale")` fires and that *one file* is
   reported as `stale` rather than clobbered. It costs nothing, since both
   functions already implement it.
5. Update the baseline entry on success.

**A failed file does not abort the sync.** Catch `JupyterError` /
`TransferConflict` / `OSError` per entry, record it in `failed`, continue.
One unreadable file must not strand the other 400. The result reports what
moved, what failed, and why:

```json
{"ok": true, "moved": {"pushed": 3, "pulled": 5, "converged": 1,
                       "deleted_local": 0, "deleted_remote": 0},
 "bytes_up": 193273528, "bytes_down": 4211,
 "skipped": 110, "conflicts_unresolved": 1,
 "failed": [{"path": "locked.db", "error": "...", "reason": "stale"}],
 "conflict_copies": ["notes.md.jsonyter-conflict-20260915T180411Z"],
 "integrity": "sha256", "elapsed": 31.8,
 "baseline": "/home/e/.local/state/jsonyter/sync/8f2c….json"}
```

`ok` is `false` when anything failed or any conflict went unresolved — a caller
must be able to branch on one field.

### 8.4 Cancellation and locking

A sync over a slow link runs for minutes, so **it must be interruptible.**
There is no per-file abort — cancelling mid-chunk buys little and complicates
`transfer.py` — but between files is cheap and sufficient. `sync_apply` polls
`should_cancel()` before each entry and, when it returns true, stops, writes
the baseline for everything that *did* complete, and returns with
`"cancelled": true`. A cancelled sync is a valid partial sync, and re-running
resumes naturally because the baseline is current.

**Lock the pair.** Two syncs of the same pair at once (two Emacs frames, a
double-tapped keybinding) would race on the baseline. Take an exclusive lock on
the state file for the duration — `O_EXCL` on `<state>.lock` holding the pid,
stale after 24 h — and raise `SyncRefused(reason="locked")` naming the holder.

## 9. Ignore patterns

`DEFAULT_IGNORE` above, plus a caller-supplied `ignore`, matched
`fnmatch`-style against the path relative to the sync root. A trailing `/`
matches a directory and prunes the whole subtree — *prunes*, not filters, so an
ignored `.git/` costs no `list_contents` requests at all.

Three entries deserve their reasons:

- **`.ipynb_checkpoints/`** — the server creates these itself, on its own
  schedule, in directories it manages. Syncing them means syncing artefacts of
  the other end's autosave, which will never converge.
- **`*.part`** — `transfer.py`'s in-flight download files. Syncing a partial
  download is meaningless, and it would race with the download writing it.
- **`*.jsonyter-conflict-*`** — §6.2's safety copies, which must not become
  traffic of their own.

Read an optional `.jsonyterignore` from the local sync root if present, one
pattern per line, `#` comments. Same syntax, no negation (`!`) in v1 — the
`.gitignore` negation rules are subtle and nobody will miss them here.

**Symlinks are skipped, not followed**, unless `follow_links=True`. The
Contents API has no symlink concept, so following one means silently
materialising its target as a regular file — and a link to a parent directory
means walking forever. Report skipped links in `warnings` so the omission is
visible.

## 10. Two things that will bite, called out now

**Notebooks in the sync set.** A `.ipynb` under sync is byte-compared like
anything else, which is right — but if JupyterLab has that notebook open, its
autosave rewrites the file server-side on its own schedule, with re-ordered
JSON keys and updated `execution_count`s. The hash changes with no human having
edited anything, and the file shows up as `remote-changed` on every sync,
forever. Transfers must use `type="file"`/`format="base64"` (as `upload`
already does) so *this* code never normalises anything, and the docstring
should say plainly that a notebook open in another client will appear to change
by itself. Suggest — do not enforce — `*.ipynb` in `.jsonyterignore` for
directories where that is happening; the notebook verbs (`read_notebook` /
`write_notebook` / `notebook_hash`) are the right tool for notebooks anyway.

**The kernel writes while you sync.** A kernel actively producing output files
in the synced directory will have files change between the scan and the
transfer. §8.3's `expect_hash` turns that into a clean per-file `stale` report
rather than a corrupted copy, and the next sync picks the file up. Do not try
to be cleverer than that — quiescing the kernel is the user's call, not this
module's.

## 11. `cli.py` wiring

Add to `_CLIENT_METHODS`, on the REST worker pool for the same reason
`upload`/`download` are: a 40-file sync must never queue behind a running
`execute`.

```python
"sync_plan":  ("local_dir", "remote_dir", "ignore", "conflict", "delete",
               "state_path", "rehash", "max_files", "max_deletes",
               "follow_links"),
"sync_apply": ("plan", "overrides", "chunk_size", "keep_conflict_copies"),
"sync":       ("local_dir", "remote_dir", "conflict", "delete", "ignore",
               "state_path", "chunk_size", "rehash"),
"sync_status": ("local_dir", "remote_dir", "state_path"),
"cancel_sync": ("request_id",),
```

Dispatch them explicitly to `sync.py` alongside the existing
`upload`/`download`/`kernel_contents_dir` special cases, passing `self.client`
and `self._progress_emitter(request_id)`.

`sync_status` is `sync_plan` with `delete="none"` and a promise of no writes —
a separate name because a front end wants an obviously-read-only verb to hang a
"what would change?" command on.

### 11.1 Progress lines: two levels, additively

A sync needs *file i of N* as well as *bytes within the current file*. Extend
the existing `progress` line with optional keys rather than inventing a second
line type — every current key keeps its meaning, so the elisp `:progress`
handler keeps working unmodified and simply ignores what it does not know:

```json
{"id": 9, "progress": {"phase": "sync", "op": "push",
                       "path": "work/data/trials.csv",
                       "local_path": "/home/e/project/data/trials.csv",
                       "bytes_done": 25165824, "bytes_total": 193273528,
                       "chunk": 3, "chunks_total": 23,
                       "file_index": 4, "files_total": 12,
                       "files_done": 3, "sync_bytes_done": 41000000,
                       "sync_bytes_total": 260000000, "elapsed": 4.12}}
```

`phase` is `"sync"` for the duration (not `"upload"`/`"download"` — a front end
showing a mode-line tag wants to say "syncing", and the direction is in `op`).
The existing `_progress_emitter` rate limit (~4/s) covers this unchanged; wrap
the per-file callable `transfer.upload` receives so it adds the sync-level
counters.

Also emit a `"phase": "scan"` progress line during §8.2's walk, with
`bytes_total: null` and a directory count. The scan of a large tree is many
seconds of apparent silence before the first transfer, and silence in a program
that is about to modify files reads as a hang.

Document both in the `cli` module docstring beside the existing shapes.

### 11.2 A one-shot CLI mode

`transfer`-style methods are stdio-driven, but a sync is the one verb that is
also useful from a shell script and a cron entry:

```
jsonyter --url ... sync ~/project/data work/data --conflict newest --delete none
```

Print a human summary, exit non-zero when `ok` is false, and add `--dry-run`
(= `sync_plan`, print the plan as a table). This costs little and makes the
feature testable by hand, which the stdio protocol alone does not.

## 12. Errors

Same three rules as the transfer work — distinguish proxy errors from Jupyter
errors, name the numbers, name the recovery — plus one specific to sync:

**Say what was and was not done.** A sync that half-completed must never report
as a flat failure; the user's next question is always "so what state am I in?"

Target shapes:

```
sync of ~/project/data <-> work/data finished with 1 conflict unresolved:
notes.md changed on both ends (local sha256 3f2a…, server 9c11…, server copy
is 30 min newer) — resolve it with conflict=newest, conflict=local,
conflict=remote, or edit one side and re-run
```

```
refusing to sync: the plan would delete 214 file(s) from the server, more than
half the 400 the last sync recorded. If ~/project/data is on an unmounted
volume this is not what you want. Re-run with max_deletes=250 to proceed.
```

```
sync moved 8 file(s) (184 MB up, 4.1 KB down) and failed on 1: locked.db
changed on the server between the scan and the transfer — re-run to pick it up
```

```
sync verified by size only, not sha256: this server predates jupyter_server
2.11 and does not return content hashes, so a same-size edit cannot be detected
```

## 13. Testing

Build on `tests/conftest.py`'s `FakeContentsServer` / `FakeSession`; no live
server on the unit path.

- **One case per row of §5's table.** Construct L/R/B, assert the plan's
  `action` and `reason`. This is the suite's backbone — fifteen small,
  exhaustive tests against a pure function.
- **Convergence.** Sync twice; the second run's plan is all `skip`/`in-sync`,
  moves zero bytes, and issues only directory listings. Then sync a third time
  with the trees swapped in the caller's arguments and assert it is still a
  no-op — a sync that is not idempotent is not a sync.
- **No ping-pong.** A file present and identical on both ends is never
  transferred in either direction, at any point.
- **Baseline integrity.** Kill `sync_apply` (raise) after file 3 of 8; assert
  the baseline records exactly those 3, that the other 5 are re-planned on the
  next run, and that no `.tmp` state file survives.
- **Conflicts.** Each policy resolves as specified; `newest` with a fabricated
  skew inside `SKEW_TOLERANCE` degrades to unresolved rather than guessing;
  `keep_conflict_copies` leaves the loser's bytes recoverable and the copy is
  ignored by the following sync.
- **Clock skew.** A `Date` header 47 s ahead flips a `newest` decision relative
  to the naive comparison; assert the corrected side wins and the skew appears
  in `warnings`.
- **Deletions.** Default policy deletes nothing; `push`/`pull`/`both` each
  delete only their side; a first sync with no baseline never deletes even
  under `both`; `max_deletes` refuses with `SyncRefused` before any request is
  issued.
- **Request economy.** Assert an exact request count for an unchanged 50-file /
  5-directory tree (it should be ~6, not ~56). Lock the number in — this is the
  §8.2 optimisation, and a regression would be invisible otherwise.
- **Hash algorithm.** A server reporting `hash_algorithm: "sha1"` is compared
  in sha1, not sha256; an unknown algorithm raises rather than mis-comparing.
  A pre-2.11 server (no `hash` key) degrades to `integrity: "size"` and says so.
- **Ignores.** `.git/` is pruned, not walked (assert no `list_contents` for it);
  `.jsonyterignore` is read; a symlink is skipped and reported.
- **Cancellation.** `should_cancel` returning true after 2 files stops there,
  reports `cancelled`, and leaves a baseline that makes the next run resume.
- **Locking.** A second concurrent `sync_apply` on the same pair raises
  `SyncRefused(reason="locked")`; a stale lock older than 24 h is broken.
- **Progress.** `files_done` is monotonic, the last event has
  `bytes_done == bytes_total` and `files_done == files_total`, a `scan` phase
  line precedes the first transfer, and the rate limit holds.

Keep one opt-in integration test against a real `jupyter server` (behind
`JSONYTER_LIVE_URL`, like `test_integration.py`) that round-trips a small tree
in both directions.

## 14. Out of scope for v1

- **Continuous watching / a daemon.** §2. The seam is `sync()` being cheap to
  call repeatedly; a watcher is a caller, not a feature of this module.
- **Recursive *transfer* as a standalone verb.** `sync` walks trees; `upload`
  and `download` stay single-file. Do not grow a second tree-walker.
- **Renames and moves as first-class operations.** A rename reads as a delete
  plus a create, which is correct, if not minimal. Detecting it by content hash
  is possible and is a natural v2 — it would turn a re-upload into a
  server-side `rename_contents`.
- **Partial-file / delta sync.** Whole files only. No rsync rolling checksums.
- **Permissions, ownership, mtime preservation.** The Contents API models none
  of them.
- **Syncing through the kernel**, for paths outside the server's `root_dir` —
  same rejection, and the same seam, as `FEATURE-REQUEST-file-transfer.md` §4.1.
- **More than two endpoints**, or a sync pair whose two sides are both remote.
