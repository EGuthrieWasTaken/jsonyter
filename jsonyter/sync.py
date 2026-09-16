"""Bidirectional directory sync between local disk and a Jupyter server.

Every sync is a discrete, on-demand call: :func:`sync_plan` compares one
local directory against one Contents-API directory and returns a plan;
:func:`sync_apply` executes that plan by calling :mod:`jsonyter.transfer` for
every byte that moves; :func:`sync` does both in one call. There is no
watching, no daemon, no polling loop — see the module-level design doc
(``FEATURE-REQUEST-directory-sync.md``) for why.

The core idea is a three-way comparison against a **baseline**: the
(path, hash) set as of the last successful sync of this pair, kept in a
small JSON file under ``$XDG_STATE_HOME/jsonyter/sync/``. Comparing local
(L) and remote (R) alone can only say "these differ", not *which side*
changed — and it's the direction of a change, not its mere existence, that
tells a routine one-sided update apart from a genuine conflict. See
:func:`_classify` for the decision table this turns into.

Hashes are sha256 by default (``GET /api/contents/<path>?content=0&hash=1``,
jupyter_server >= 2.11, which hashes server-side without transferring the
file), read back per-server via ``hash_algorithm`` rather than assumed. A
server that predates that endpoint degrades the whole run to a ``(size,
mtime)`` comparison, reported as ``integrity: "size"``.

Deletions are opt-in (``delete="none"`` by default) and only ever
considered once a baseline exists, since without one a missing path can't
be told apart from a not-yet-created one. A bulk-deletion guard
(:data:`DEFAULT_MAX_DELETES`) refuses to act on a plan that would delete an
implausible number of files — the signature of an unmounted volume or a
wrong ``local_dir``, not of real intent.
"""

import fnmatch
import hashlib
import json
import os
import posixpath
import time
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from . import transfer
from .client import JupyterError
from .transfer import _clean_remote, _resolve_local

DEFAULT_IGNORE = (
    ".git/", ".hg/", ".svn/", "__pycache__/", ".ipynb_checkpoints/",
    ".DS_Store", "*.pyc", "*.pyo", "*.swp", "*~", "*.part",
    "*.jsonyter-conflict-*", ".jsonyterignore",
)
SKEW_TOLERANCE = 5.0        # seconds; below this, "newest" will not guess
DEFAULT_MAX_DELETES = 25
DEFAULT_MAX_FILES = 5000    # a listing guard, not a transfer guard

CONFLICT_POLICIES = ("ask", "newest", "local", "remote", "skip")
DELETE_POLICIES = ("none", "push", "pull", "both")

_BASELINE_VERSION = 1
_LOCK_STALE_AFTER = 24 * 3600
_CONFLICT_SUFFIX = ".jsonyter-conflict-"

# Reasons, one-to-one with the table in the module docstring / feature
# request §5. The front end formats these; it must never parse prose.
_REASON_LOCAL_NEW = "local-new"
_REASON_REMOTE_NEW = "remote-new"
_REASON_IDENTICAL = "identical"
_REASON_BOTH_CHANGED = "both-changed"
_REASON_UNCHANGED = "unchanged"
_REASON_LOCAL_CHANGED = "local-changed"
_REASON_REMOTE_CHANGED = "remote-changed"
_REASON_LOCAL_MISSING = "local-missing"
_REASON_REMOTE_MISSING = "remote-missing"
_REASON_IGNORED = "ignored"


class SyncRefused(JupyterError):
    """The plan was not safe to build or run; nothing was changed.

    ``reason`` is one of ``"too-many-deletes"``, ``"too-many-files"``,
    ``"locked"``, ``"algorithm-mismatch"``. Carries the numbers and the name
    of the parameter that overrides the guard, in the message.
    """

    def __init__(self, message, reason=None, **extra):
        super().__init__(message)
        self.reason = reason
        self.extra = extra

    def to_json(self):
        payload = super().to_json()
        payload["reason"] = self.reason
        payload.update(self.extra)
        return payload


# ------------------------------------------------------------------ baseline

def _state_dir():
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser(
        "~/.local/state")
    return os.path.join(base, "jsonyter", "sync")


def default_state_path(server, local_dir, remote_dir):
    key = "\0".join([server, remote_dir, local_dir]).encode("utf-8")
    digest = hashlib.sha256(key).hexdigest()[:16]
    return os.path.join(_state_dir(), digest + ".json")


def _load_baseline(path):
    """The stored baseline, or ``(None, warning)`` for missing/corrupt.

    A missing or unreadable baseline is not an error (§5.2): it is treated
    as a first sync. Discarding it is always a safe recovery action.
    """
    if not path or not os.path.exists(path):
        return None, None
    try:
        with open(path, "r") as handle:
            data = json.load(handle)
        if not isinstance(data, dict) or data.get("version") != _BASELINE_VERSION:
            raise ValueError("unrecognized baseline shape")
        if not isinstance(data.get("entries"), dict):
            raise ValueError("no entries")
        return data, None
    except (ValueError, OSError, TypeError) as exc:
        return None, ("baseline at {} could not be read ({}); treating this "
                      "as a first sync".format(path, exc))


def _save_baseline(path, data):
    """Write atomically: tmp + os.replace, so a crash never half-writes."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp-" + uuid.uuid4().hex
    with open(tmp, "w") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
    os.replace(tmp, path)


class _Lock:
    """An exclusive, ``O_EXCL``-based lock on ``<state>.lock``.

    Stale after :data:`_LOCK_STALE_AFTER`, so a crashed process doesn't wedge
    the pair forever.
    """

    def __init__(self, state_path):
        self.path = state_path + ".lock"
        self._held = False

    def acquire(self):
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as handle:
                handle.write(str(os.getpid()))
            self._held = True
            return
        except FileExistsError:
            pass
        try:
            age = time.time() - os.path.getmtime(self.path)
        except OSError:
            age = None
        if age is not None and age > _LOCK_STALE_AFTER:
            try:
                os.remove(self.path)
            except OSError:
                pass
            return self.acquire()
        holder = None
        try:
            with open(self.path) as handle:
                holder = handle.read().strip()
        except OSError:
            pass
        raise SyncRefused(
            "another sync of this pair is already running (lock held by "
            "pid {}); wait for it to finish, or remove {} if it is stale"
            .format(holder or "?", self.path),
            reason="locked", holder=holder)

    def release(self):
        if self._held:
            try:
                os.remove(self.path)
            except OSError:
                pass
            self._held = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


# -------------------------------------------------------------------- ignore

def _read_ignore_file(local_dir):
    path = os.path.join(local_dir, ".jsonyterignore")
    if not os.path.isfile(path):
        return ()
    patterns = []
    with open(path) as handle:
        for line in handle:
            line = line.strip()
            if line and not line.startswith("#"):
                patterns.append(line)
    return tuple(patterns)


def _is_ignored(relpath, is_dir, patterns):
    name = relpath.rsplit("/", 1)[-1]
    for pat in patterns:
        if pat.endswith("/"):
            if is_dir and fnmatch.fnmatch(name, pat[:-1]):
                return True
            continue
        if fnmatch.fnmatch(relpath, pat) or fnmatch.fnmatch(name, pat):
            return True
    return False


# ------------------------------------------------------------------- scanning

def _walk_local(local_dir, patterns, follow_links, max_files, warnings):
    """``{relpath: {"size", "mtime", "inode"}}`` — free metadata via scandir.

    Symlinks are skipped, not followed, unless ``follow_links`` — the
    Contents API has no symlink concept, and a link to a parent directory
    means walking forever.
    """
    entries = {}
    count = 0

    def _scan(abs_dir, rel_dir):
        nonlocal count
        try:
            children = sorted(os.scandir(abs_dir), key=lambda e: e.name)
        except OSError as exc:
            warnings.append("could not list {}: {}".format(abs_dir, exc))
            return
        for entry in children:
            rel = rel_dir + "/" + entry.name if rel_dir else entry.name
            try:
                is_link = entry.is_symlink()
            except OSError:
                continue
            if is_link and not follow_links:
                warnings.append("skipped symlink: {}".format(rel))
                continue
            try:
                is_dir = entry.is_dir(follow_symlinks=follow_links)
            except OSError:
                continue
            if is_dir:
                if _is_ignored(rel, True, patterns):
                    continue
                _scan(entry.path, rel)
            else:
                if _is_ignored(rel, False, patterns):
                    continue
                try:
                    st = entry.stat(follow_symlinks=follow_links)
                except OSError as exc:
                    warnings.append("could not stat {}: {}".format(rel, exc))
                    continue
                count += 1
                if count > max_files:
                    raise SyncRefused(
                        "the local tree under {} has more than {} file(s); "
                        "this guards against a local_dir pointed somewhere "
                        "far larger than intended — pass a higher max_files "
                        "if it really is this large".format(
                            local_dir, max_files),
                        reason="too-many-files", max_files=max_files)
                entries[rel] = {"size": st.st_size, "mtime": st.st_mtime,
                                "inode": st.st_ino}

    _scan(local_dir, "")
    return entries


def _list_contents_raw(client, path, timeout):
    """Like ``client.list_contents`` but exposes headers (for clock skew).

    Returns ``(model_or_None, headers)``; ``None`` means "no such directory"
    (404), which is not an error — the remote side of a first sync may not
    exist yet.
    """
    resp = client._request_raw(
        "GET", "/api/contents/" + path.lstrip("/"), params={"content": "1"},
        timeout=timeout)
    if resp.status_code == 404:
        return None, resp.headers
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("message", resp.text)
        except ValueError:
            detail = resp.text
        raise JupyterError(detail, status=resp.status_code, url=resp.url,
                           cf_ray=resp.headers.get("cf-ray"))
    return resp.json(), resp.headers


def _walk_remote(client, remote_dir, patterns, max_files, timeout, warnings):
    """``({relpath: {"size", "mtime"}}, dir_count, request_count, skew)``.

    One ``list_contents`` per directory; directories matching ``patterns``
    are pruned — never listed at all. Clock skew (§6.1) is sampled once,
    off the first response's ``Date`` header.
    """
    entries = {}
    dir_count = 0
    request_count = 0
    count = 0
    skew = None
    stack = [remote_dir]
    remote_root_norm = remote_dir.strip("/")
    while stack:
        cur = stack.pop()
        request_count += 1
        received_at = time.time()
        model, headers = _list_contents_raw(client, cur, timeout)
        if skew is None:
            skew = _skew_from_header(headers.get("Date"), received_at)
        if model is None:
            continue
        if model.get("type") != "directory":
            if cur == remote_dir:
                raise JupyterError(
                    "{} is a file on the server, not a directory — cannot "
                    "sync a directory against it".format(remote_dir))
            continue
        dir_count += 1
        for child in model.get("content") or []:
            cpath = (child.get("path") or "").strip("/")
            if remote_root_norm:
                if cpath == remote_root_norm:
                    rel = ""
                elif cpath.startswith(remote_root_norm + "/"):
                    rel = cpath[len(remote_root_norm) + 1:]
                else:
                    rel = cpath
            else:
                rel = cpath
            is_dir = child.get("type") == "directory"
            if _is_ignored(rel, is_dir, patterns):
                continue
            if is_dir:
                stack.append(cpath)
            else:
                count += 1
                if count > max_files:
                    raise SyncRefused(
                        "the remote tree under {} has more than {} file(s); "
                        "pass a higher max_files if it really is this "
                        "large".format(remote_dir, max_files),
                        reason="too-many-files", max_files=max_files)
                entries[rel] = {"size": child.get("size"),
                                "mtime": child.get("last_modified")}
    return entries, dir_count, request_count, skew


def _skew_from_header(date_header, received_at):
    if not date_header:
        return None
    try:
        dt = parsedate_to_datetime(date_header)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp() - received_at


def _parse_iso(ts):
    if not ts:
        return None
    text = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


# --------------------------------------------------------------- fingerprints

class _Integrity:
    """Tracks the sha256-vs-size degradation (§4) across one sync run."""

    def __init__(self):
        self.algo = None            # learned lazily from the first hash model
        self.mode = "sha256"        # "sha256" until a server proves otherwise
        self.requests = 0

    def note_model(self, model):
        server_hash = model.get("hash")
        algo = model.get("hash_algorithm") or "sha256"
        if server_hash is None:
            self.mode = "size"
            return None
        if self.algo is None:
            self.algo = algo
        elif self.algo != algo:
            raise SyncRefused(
                "the server reported hash_algorithm {!r} but earlier in "
                "this same sync reported {!r} — refusing to compare "
                "digests computed under two different algorithms"
                .format(algo, self.algo), reason="algorithm-mismatch")
        return server_hash

    def local_digest(self, path):
        algo = self.algo or "sha256"
        try:
            digest = hashlib.new(algo)
        except ValueError:
            raise SyncRefused(
                "the server's hash_algorithm {!r} is not one hashlib knows "
                "about; local files cannot be hashed comparably"
                .format(algo), reason="algorithm-mismatch")
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()


def _local_fingerprint(local_path, meta, baseline_entry, rehash, integrity):
    if meta is None:
        return None
    if integrity.mode == "size":
        return meta["size"]
    if (not rehash and baseline_entry is not None
            and baseline_entry.get("size") == meta["size"]
            and baseline_entry.get("local_mtime") == meta["mtime"]
            and baseline_entry.get("local_inode") == meta["inode"]
            and baseline_entry.get("hash") is not None):
        return baseline_entry["hash"]
    return integrity.local_digest(local_path)


def _remote_fingerprint(client, remote_path, meta, baseline_entry, rehash,
                        integrity, timeout):
    if meta is None:
        return None
    if integrity.mode == "size":
        return meta["size"]
    if (not rehash and baseline_entry is not None
            and baseline_entry.get("size") == meta["size"]
            and baseline_entry.get("remote_mtime") == meta["mtime"]
            and baseline_entry.get("hash") is not None):
        return baseline_entry["hash"]
    model = client.get_contents(remote_path, content=False, hash=True,
                                timeout=timeout)
    integrity.requests += 1
    digest = integrity.note_model(model)
    if digest is None:
        return meta["size"]
    return digest


# ---------------------------------------------------------------- the table

def _classify(l_fp, r_fp, b_fp, delete_policy):
    """The decision table (module docstring / feature request §5), as a
    pure function of three fingerprints (a hash, a size under size-only
    integrity, or ``None`` for "absent"). Returns ``(action, reason)``.
    """
    if b_fp is None:
        if l_fp is not None and r_fp is None:
            return "push", _REASON_LOCAL_NEW
        if l_fp is None and r_fp is not None:
            return "pull", _REASON_REMOTE_NEW
        if l_fp is not None and r_fp is not None:
            if l_fp == r_fp:
                return "converge", _REASON_IDENTICAL
            return "conflict", _REASON_BOTH_CHANGED
        return "skip", _REASON_UNCHANGED
    if l_fp is not None and r_fp is not None:
        l_same = l_fp == b_fp
        r_same = r_fp == b_fp
        if l_same and r_same:
            return "skip", _REASON_UNCHANGED
        if not l_same and r_same:
            return "push", _REASON_LOCAL_CHANGED
        if l_same and not r_same:
            return "pull", _REASON_REMOTE_CHANGED
        if l_fp == r_fp:
            return "converge", _REASON_BOTH_CHANGED
        return "conflict", _REASON_BOTH_CHANGED
    if l_fp is None and r_fp is not None:
        if r_fp == b_fp:
            action = "push-delete" if delete_policy in ("push", "both") else "skip"
            return action, _REASON_LOCAL_MISSING
        # Deleted locally, but the remote copy moved since the baseline: a
        # genuine conflict regardless of delete policy — honouring the
        # deletion would silently discard the remote edit.
        return "conflict", _REASON_LOCAL_MISSING
    if l_fp is not None and r_fp is None:
        if l_fp == b_fp:
            action = "pull-delete" if delete_policy in ("pull", "both") else "skip"
            return action, _REASON_REMOTE_MISSING
        return "conflict", _REASON_REMOTE_MISSING
    return "forget", _REASON_UNCHANGED


# ------------------------------------------------------------------- planning

def sync_plan(client, local_dir, remote_dir, *, ignore=None, follow_links=False,
             conflict="ask", delete="none", state_path=None, rehash=False,
             max_files=DEFAULT_MAX_FILES, max_deletes=DEFAULT_MAX_DELETES,
             timeout=None, progress=None):
    """Compare both trees against the baseline; move nothing.

    Read-only on both ends and on the baseline — safe to call at any time.
    Returns the plan described in the module docstring / feature request
    §8.1: ``local_dir``, ``remote_dir``, ``server``, ``hash_algorithm``,
    ``integrity``, ``baseline``, ``clock_skew``, ``scanned``, ``entries``,
    ``totals``, ``warnings``.
    """
    if conflict not in CONFLICT_POLICIES:
        raise JupyterError(
            "conflict must be one of {}, got {!r}".format(
                CONFLICT_POLICIES, conflict))
    if delete not in DELETE_POLICIES:
        raise JupyterError(
            "delete must be one of {}, got {!r}".format(DELETE_POLICIES, delete))

    local = _resolve_local(local_dir)
    if not os.path.isdir(local):
        raise JupyterError(
            "no such local directory: {} — if this is on a removable or "
            "network volume, check that it is actually mounted before "
            "syncing (an unmounted mountpoint looks like an empty "
            "directory, which is exactly the case the delete-policy "
            "guards exist for)".format(local))
    if not isinstance(remote_dir, str):
        raise JupyterError("missing or invalid param: remote_dir")
    remote = _clean_remote(remote_dir) if remote_dir.strip("/") else ""
    server = client.base_url
    path = state_path or default_state_path(server, local, remote)
    warnings = []

    baseline, baseline_warning = _load_baseline(path)
    if baseline_warning:
        warnings.append(baseline_warning)
    baseline_entries = (baseline or {}).get("entries", {})

    patterns = tuple(ignore) if ignore is not None else ()
    patterns = patterns + DEFAULT_IGNORE + _read_ignore_file(local)

    if progress is not None:
        progress({"phase": "scan", "op": "local", "bytes_total": None})
    local_meta = _walk_local(local, patterns, follow_links, max_files, warnings)
    if progress is not None:
        progress({"phase": "scan", "op": "remote", "bytes_total": None})
    remote_meta, dir_count, request_count, skew = _walk_remote(
        client, remote, patterns, max_files, timeout, warnings)

    if skew is not None and abs(skew) >= 1.0:
        warnings.append(
            "clock skew vs the server is {:+.0f} s".format(skew))

    integrity = _Integrity()
    if baseline is not None and baseline.get("hash_algorithm"):
        # The algorithm a server uses is a deployment setting, not something
        # that changes between syncs of the same pair — assume it is still
        # what the baseline last saw, so the *first* local file hashed this
        # run already uses the right algorithm instead of guessing sha256
        # and getting it wrong until some remote file happens to teach us.
        integrity.algo = baseline["hash_algorithm"]
    all_paths = sorted(set(local_meta) | set(remote_meta) | set(baseline_entries))

    entries = []
    totals = {"push": 0, "pull": 0, "converge": 0, "skip": 0, "conflict": 0,
              "push_delete": 0, "pull_delete": 0, "forget": 0,
              "bytes_up": 0, "bytes_down": 0}
    n_close_conflicts = 0

    for rel in all_paths:
        lm = local_meta.get(rel)
        rm = remote_meta.get(rel)
        be = baseline_entries.get(rel)
        b_fp = be.get("hash") if be else None

        local_full = os.path.join(local, *rel.split("/")) if rel else local
        remote_full = (remote + "/" + rel) if remote else rel
        # Remote first: hashing the remote side (when needed) is what
        # teaches ``integrity`` the server's real hash_algorithm, and the
        # local side must be hashed with that same algorithm rather than a
        # guessed default.
        r_fp = _remote_fingerprint(client, remote_full, rm, be, rehash,
                                   integrity, timeout)
        l_fp = _local_fingerprint(local_full, lm, be, rehash, integrity)

        action, reason = _classify(l_fp, r_fp, b_fp, delete)

        entry = {
            "path": rel, "action": action, "reason": reason,
            "local": ({"size": lm["size"], "hash": l_fp if integrity.mode ==
                      "sha256" else None, "mtime": lm["mtime"]}
                      if lm else None),
            "remote": ({"size": rm["size"], "hash": r_fp if integrity.mode ==
                       "sha256" else None, "mtime": rm["mtime"]}
                       if rm else None),
            "baseline_hash": b_fp,
        }

        if action == "conflict":
            resolution, newest_side, delta, close_call = _resolve_conflict(
                lm, rm, conflict, skew)
            entry["resolution"] = resolution
            entry["newest"] = newest_side
            entry["mtime_delta"] = delta
            if close_call:
                n_close_conflicts += 1
            # A resolution that actually moves or deletes something changes
            # the *action*; "skip" (an explicit policy choice to leave both
            # sides alone) and None (ask: the front end decides) both leave
            # this path classified as an unresolved conflict, since neither
            # one does anything to it.
            if resolution not in (None, "skip"):
                action = resolution
                entry["action"] = resolution

        if action == "push":
            entry["bytes"] = lm["size"] if lm else 0
            totals["bytes_up"] += entry["bytes"]
        elif action == "pull":
            entry["bytes"] = rm["size"] if rm else 0
            totals["bytes_down"] += entry["bytes"]

        key = {"push": "push", "pull": "pull", "converge": "converge",
              "skip": "skip", "conflict": "conflict",
              "push-delete": "push_delete", "pull-delete": "pull_delete",
              "forget": "forget"}[action]
        totals[key] += 1
        entries.append(entry)

    n_deletes = totals["push_delete"] + totals["pull_delete"]
    baseline_size = len(baseline_entries)
    if n_deletes > max_deletes:
        half_note = ""
        if baseline_size and n_deletes > baseline_size / 2:
            half_note = " (more than half of the {} the last sync recorded)" \
                .format(baseline_size)
        raise SyncRefused(
            "refusing to sync: this plan would delete {} file(s) from {}, "
            "more than max_deletes={}{}. If {} is on an unmounted volume, "
            "or local_dir/remote_dir is wrong, this is not what you want. "
            "Re-run sync_plan with a higher max_deletes to proceed.".format(
                n_deletes,
                "the server" if totals["push_delete"] >= totals["pull_delete"]
                else "local disk",
                max_deletes, half_note, local),
            reason="too-many-deletes", count=n_deletes, max_deletes=max_deletes)

    if n_close_conflicts:
        warnings.append(
            "{} conflict(s) were too close to call (within {}s of the "
            "clock skew) and were left unresolved".format(
                n_close_conflicts, SKEW_TOLERANCE))

    return {
        "local_dir": local, "remote_dir": remote, "server": server,
        "state_path": path, "hash_algorithm": integrity.algo or "sha256",
        "integrity": integrity.mode, "baseline": "present" if baseline else "absent",
        "clock_skew": skew, "conflict_policy": conflict, "delete_policy": delete,
        "scanned": {"local": len(local_meta), "remote": len(remote_meta),
                   "directories": dir_count,
                   "requests": request_count + integrity.requests},
        "entries": entries, "totals": totals, "warnings": warnings,
    }


def _resolve_conflict(lm, rm, policy, skew):
    """``(resolution, newest_side, mtime_delta, close_call)``.

    ``resolution`` is ``None`` (stays a conflict) or one of ``"push"`` /
    ``"pull"`` / ``"push-delete"`` / ``"pull-delete"`` / ``"skip"``. Exactly
    one of ``lm``/``rm`` is ``None`` for a deletion-vs-edit conflict; both
    are given for a content-vs-content one.

    ``newest``/``mtime_delta`` are precomputed for every conflict (even under
    ``ask``) so a front end can show "the server's copy is N newer" without
    redoing the arithmetic. ``close_call`` is True when ``newest`` refused to
    guess because the two mtimes were within the noise (§6.1) — a deletion,
    which has no timestamp of its own, also makes ``newest`` refuse, but
    that is not a "close call" and is not counted as one.
    """
    newest_side = None
    delta = None
    if lm is not None and rm is not None:
        l_mtime = lm.get("mtime")
        r_mtime = _parse_iso(rm.get("mtime"))
        if l_mtime is not None and r_mtime is not None:
            r_corrected = r_mtime - (skew or 0.0)
            delta = round(r_corrected - l_mtime, 3)
            tolerance = max(2 * abs(skew or 0.0), SKEW_TOLERANCE)
            if abs(delta) >= tolerance:
                newest_side = "remote" if delta > 0 else "local"

    if policy == "skip":
        return "skip", newest_side, delta, False
    if policy == "local":
        return ("push-delete" if lm is None else "push"), newest_side, \
            delta, False
    if policy == "remote":
        return ("pull-delete" if rm is None else "pull"), newest_side, \
            delta, False
    if policy == "newest":
        if lm is None or rm is None:
            # No timestamp exists for "when was it deleted" — refuse to guess.
            return None, newest_side, delta, False
        if newest_side is None:
            return None, newest_side, delta, True     # within the noise
        return ("pull" if newest_side == "remote" else "push"), newest_side, \
            delta, False
    return None, newest_side, delta, False


# -------------------------------------------------------------------- apply

def _conflict_suffix():
    return _CONFLICT_SUFFIX + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def _rename_aside_local(path):
    if not os.path.exists(path):
        return None
    dest = path + _conflict_suffix()
    os.replace(path, dest)
    return dest


def _rename_aside_remote(client, path, timeout):
    try:
        client.get_contents(path, content=False, timeout=timeout)
    except JupyterError as exc:
        if exc.status == 404:
            return None
        raise
    dest = path + _conflict_suffix()
    client.rename_contents(path, dest, timeout=timeout)
    return dest


def _wrap_progress(progress, op, file_index, files_total, sync_state):
    if progress is None:
        return None

    def emit(event):
        done = event.get("bytes_done") or 0
        progress(dict(event, phase="sync", op=op,
                     file_index=file_index, files_total=files_total,
                     files_done=sync_state["files_done"],
                     sync_bytes_done=sync_state["bytes_done"] + done,
                     sync_bytes_total=sync_state["bytes_total"]))

    return emit


def sync_apply(client, plan, *, overrides=None, chunk_size=None,
              keep_conflict_copies=True, progress=None, should_cancel=None,
              timeout=None):
    """Execute a plan from :func:`sync_plan`.

    ``overrides`` maps a relative path to a replacement action (``"push"``/
    ``"pull"``/``"skip"``/``"push-delete"``/``"pull-delete"``), which is how
    an interactive front end resolves ``ask`` conflicts and countermands
    individual rows. ``should_cancel`` is polled between files: when it
    returns true the run stops, the baseline for everything that *did*
    complete is written, and the result carries ``"cancelled": true``.

    A failed file does not abort the run — it is recorded in ``failed`` and
    the rest proceeds — since one unreadable file must not strand the
    others. Returns the shape in the module docstring / feature request
    §8.3.
    """
    started = time.monotonic()
    overrides = overrides or {}
    local_dir = plan["local_dir"]
    remote_dir = plan["remote_dir"]
    server = plan["server"]
    state_path = plan.get("state_path") or default_state_path(
        server, local_dir, remote_dir)
    integrity_mode = plan.get("integrity", "sha256")

    with _Lock(state_path):
        baseline, _warn = _load_baseline(state_path)
        if baseline is None:
            baseline = {"version": _BASELINE_VERSION, "server": server,
                       "local_dir": local_dir, "remote_dir": remote_dir,
                       "hash_algorithm": plan.get("hash_algorithm") or "sha256",
                       "entries": {}}
        entries_state = baseline["entries"]

        # Resolve overrides up front, so ordering and totals reflect what
        # will actually happen.
        resolved = []
        for entry in plan["entries"]:
            rel = entry["path"]
            action = overrides.get(rel, entry["action"])
            originally_conflict = (entry["action"] == "conflict"
                                   or entry.get("resolution") not in
                                   (None, "skip"))
            resolved.append((entry, action, originally_conflict))

        def _sort_key(item):
            _entry, action, _oc = item
            pull_first = 0 if action in ("pull", "pull-delete") else 1
            return (pull_first, item[0]["path"])

        resolved.sort(key=_sort_key)

        files_total = sum(1 for _e, a, _oc in resolved if a in ("push", "pull"))
        bytes_total = sum(e.get("bytes") or 0 for e, a, _oc in resolved
                          if a in ("push", "pull"))
        sync_state = {"files_done": 0, "bytes_done": 0,
                     "bytes_total": bytes_total}

        moved = {"pushed": 0, "pulled": 0, "converged": 0,
                "deleted_local": 0, "deleted_remote": 0}
        bytes_up = 0
        bytes_down = 0
        skipped = 0
        conflicts_unresolved = 0
        failed = []
        conflict_copies = []
        cancelled = False
        known_local_dirs = set()
        known_remote_dirs = set()
        file_index = 0

        def ensure_local_dir(rel):
            d = os.path.dirname(rel)
            if not d or d in known_local_dirs:
                return
            os.makedirs(os.path.join(local_dir, *d.split("/")), exist_ok=True)
            known_local_dirs.add(d)

        def ensure_remote_dir(rel):
            d = posixpath.dirname(rel)
            if not d:
                return
            cur = ""
            for part in d.split("/"):
                cur = cur + "/" + part if cur else part
                if cur in known_remote_dirs:
                    continue
                full_dir = (remote_dir + "/" + cur) if remote_dir else cur
                try:
                    client.make_directory(full_dir, timeout=timeout)
                except JupyterError:
                    # If it genuinely isn't there, the file PUT that follows
                    # will fail loudly and be reported in ``failed`` — a
                    # spurious error here (already exists, a stray 400)
                    # must not abort the whole sync over one directory.
                    pass
                known_remote_dirs.add(cur)

        for entry, action, originally_conflict in resolved:
            if should_cancel is not None and should_cancel():
                cancelled = True
                break

            rel = entry["path"]
            local_full = os.path.join(local_dir, *rel.split("/"))
            remote_full = (remote_dir + "/" + rel) if remote_dir else rel

            try:
                if action == "conflict":
                    conflicts_unresolved += 1
                    continue
                if action == "skip":
                    skipped += 1
                    continue
                if action == "forget":
                    entries_state.pop(rel, None)
                    continue
                if action == "converge":
                    st = os.stat(local_full) if os.path.exists(local_full) \
                        else None
                    hash_ = (entry["local"] or entry["remote"] or {}).get("hash") \
                        if integrity_mode == "sha256" else None
                    size_ = (entry["local"] or entry["remote"] or {}).get("size")
                    entries_state[rel] = {
                        "hash": hash_, "size": size_,
                        "local_mtime": st.st_mtime if st else None,
                        "local_inode": st.st_ino if st else None,
                        "remote_mtime": (entry["remote"] or {}).get("mtime"),
                    }
                    moved["converged"] += 1
                    _save_baseline(state_path, baseline)
                    continue

                if action in ("push", "pull") and originally_conflict \
                        and keep_conflict_copies:
                    if action == "push":
                        dest = _rename_aside_remote(client, remote_full, timeout)
                    else:
                        dest = _rename_aside_local(local_full)
                    if dest:
                        conflict_copies.append(dest)
                elif action in ("push-delete", "pull-delete") \
                        and originally_conflict and keep_conflict_copies:
                    if action == "push-delete":
                        dest = _rename_aside_remote(client, remote_full, timeout)
                    else:
                        dest = _rename_aside_local(local_full)
                    if dest:
                        conflict_copies.append(dest)
                    entries_state.pop(rel, None)
                    if action == "push-delete":
                        moved["deleted_remote"] += 1
                    else:
                        moved["deleted_local"] += 1
                    _save_baseline(state_path, baseline)
                    continue

                if action == "push":
                    file_index += 1
                    ensure_remote_dir(rel)
                    expect = ((entry.get("remote") or {}).get("hash")
                             if integrity_mode == "sha256" else None)
                    result = transfer.upload(
                        client, local_full, remote_full,
                        chunk_size=chunk_size or transfer.DEFAULT_CHUNK_SIZE,
                        overwrite=True, expect_hash=expect,
                        progress=_wrap_progress(
                            progress, "push", file_index, files_total,
                            sync_state))
                    st = os.stat(local_full)
                    # One extra request, paid once, so the *next* sync's
                    # trust cache hits this file instead of re-hashing it
                    # forever — the upload response carries no
                    # last_modified of its own.
                    try:
                        fresh = client.get_contents(remote_full, content=False,
                                                    timeout=timeout)
                        remote_mtime = fresh.get("last_modified")
                    except JupyterError:
                        remote_mtime = None
                    entries_state[rel] = {
                        "hash": result["hash"], "size": result["bytes"],
                        "local_mtime": st.st_mtime, "local_inode": st.st_ino,
                        "remote_mtime": remote_mtime,
                    }
                    moved["pushed"] += 1
                    bytes_up += result["bytes"]
                    sync_state["files_done"] += 1
                    sync_state["bytes_done"] += result["bytes"]
                elif action == "pull":
                    file_index += 1
                    ensure_local_dir(rel)
                    expect = ((entry.get("remote") or {}).get("hash")
                             if integrity_mode == "sha256" else None)
                    result = transfer.download(
                        client, remote_full, local_full, overwrite=True,
                        expect_hash=expect,
                        progress=_wrap_progress(
                            progress, "pull", file_index, files_total,
                            sync_state))
                    st = os.stat(local_full)
                    entries_state[rel] = {
                        "hash": result["hash"], "size": result["bytes"],
                        "local_mtime": st.st_mtime, "local_inode": st.st_ino,
                        "remote_mtime": (entry.get("remote") or {}).get("mtime"),
                    }
                    moved["pulled"] += 1
                    bytes_down += result["bytes"]
                    sync_state["files_done"] += 1
                    sync_state["bytes_done"] += result["bytes"]
                elif action == "push-delete":
                    try:
                        client.delete_contents(remote_full, timeout=timeout)
                    except JupyterError as exc:
                        if exc.status != 404:
                            raise
                    entries_state.pop(rel, None)
                    moved["deleted_remote"] += 1
                elif action == "pull-delete":
                    if os.path.exists(local_full):
                        os.remove(local_full)
                    entries_state.pop(rel, None)
                    moved["deleted_local"] += 1
                _save_baseline(state_path, baseline)
            except (JupyterError, OSError) as exc:
                failed.append({"path": rel, "error": str(exc),
                             "reason": getattr(exc, "reason", None)})

        baseline["synced_at"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds").replace("+00:00", "Z")
        baseline["hash_algorithm"] = plan.get("hash_algorithm") or \
            baseline.get("hash_algorithm") or "sha256"
        baseline["server"] = server
        baseline["local_dir"] = local_dir
        baseline["remote_dir"] = remote_dir
        _save_baseline(state_path, baseline)

    ok = not failed and conflicts_unresolved == 0
    result = {
        "ok": ok, "moved": moved, "bytes_up": bytes_up, "bytes_down": bytes_down,
        "skipped": skipped, "conflicts_unresolved": conflicts_unresolved,
        "failed": failed, "conflict_copies": conflict_copies,
        "integrity": integrity_mode, "elapsed": round(
            time.monotonic() - started, 3),
        "baseline": state_path,
    }
    if cancelled:
        result["cancelled"] = True
    return result


def sync(client, local_dir, remote_dir, *, conflict="newest", **kwargs):
    """``sync_plan`` + ``sync_apply`` in one call, for non-interactive use.

    Note the different ``conflict`` default: a caller that cannot answer a
    question should not be asked one.
    """
    apply_keys = ("overrides", "chunk_size", "keep_conflict_copies",
                 "should_cancel")
    apply_kwargs = {k: kwargs.pop(k) for k in apply_keys if k in kwargs}
    progress = kwargs.pop("progress", None)
    # ``timeout`` applies to both phases, so it stays in ``kwargs`` for
    # ``sync_plan`` below *and* is copied here for ``sync_apply``.
    apply_kwargs["timeout"] = kwargs.get("timeout")
    plan = sync_plan(client, local_dir, remote_dir, conflict=conflict,
                     progress=progress, **kwargs)
    return sync_apply(client, plan, progress=progress, **apply_kwargs)


def sync_status(client, local_dir, remote_dir, *, state_path=None, timeout=None,
                progress=None):
    """``sync_plan`` with ``delete="none"`` and a promise of no writes.

    A separate name so a front end has an obviously-read-only verb to hang a
    "what would change?" command on.
    """
    return sync_plan(client, local_dir, remote_dir, delete="none",
                     state_path=state_path, timeout=timeout, progress=progress)
