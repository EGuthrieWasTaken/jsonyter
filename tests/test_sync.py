"""``jsonyter.sync`` — the decision table, conflicts, deletions, hashing,
ignores, cancellation, locking and request economy."""

import os
import time

import pytest

from jsonyter import JupyterError
from jsonyter.sync import (
    SyncRefused, _classify, _Integrity, _load_baseline, sync, sync_apply,
    sync_plan,
)
from conftest import build_client

B, L, R = "b-hash", "l-hash", "r-hash"


def _state(tmp_path, name="baseline.json"):
    return str(tmp_path / name)


def _write(path, data):
    with open(path, "wb") as handle:
        handle.write(data if isinstance(data, bytes) else data.encode())


# --------------------------------------------------------- the decision table

@pytest.mark.parametrize("l_fp, r_fp, b_fp, delete, action, reason", [
    # no baseline
    (L, None, None, "none", "push", "local-new"),
    (None, R, None, "none", "pull", "remote-new"),
    ("x", "x", None, "none", "converge", "identical"),
    (L, R, None, "none", "conflict", "both-changed"),
    # baseline present, both sides present
    (B, B, B, "none", "skip", "unchanged"),
    (L, B, B, "none", "push", "local-changed"),
    (B, R, B, "none", "pull", "remote-changed"),
    ("x", "x", B, "none", "converge", "both-changed"),
    (L, R, B, "none", "conflict", "both-changed"),
    # baseline present, local missing
    (None, B, B, "none", "skip", "local-missing"),
    (None, B, B, "push", "push-delete", "local-missing"),
    (None, B, B, "both", "push-delete", "local-missing"),
    (None, R, B, "none", "conflict", "local-missing"),
    (None, R, B, "push", "conflict", "local-missing"),
    # baseline present, remote missing
    (B, None, B, "none", "skip", "remote-missing"),
    (B, None, B, "pull", "pull-delete", "remote-missing"),
    (B, None, B, "both", "pull-delete", "remote-missing"),
    (L, None, B, "none", "conflict", "remote-missing"),
    (L, None, B, "pull", "conflict", "remote-missing"),
    # both gone
    (None, None, B, "none", "forget", "unchanged"),
])
def test_classify_decision_table(l_fp, r_fp, b_fp, delete, action, reason):
    assert _classify(l_fp, r_fp, b_fp, delete) == (action, reason)


# ------------------------------------------------------------------- pushing

def test_first_sync_pushes_new_local_file(tmp_path):
    client, server = build_client()
    local = tmp_path / "local"
    local.mkdir()
    _write(local / "a.txt", "hello")

    plan = sync_plan(client, str(local), "work/data", state_path=_state(tmp_path))
    assert plan["baseline"] == "absent"
    assert len(plan["entries"]) == 1
    entry = plan["entries"][0]
    assert entry["action"] == "push" and entry["reason"] == "local-new"

    result = sync_apply(client, plan)
    assert result["ok"]
    assert result["moved"]["pushed"] == 1
    assert bytes(server.files["work/data/a.txt"]) == b"hello"


def test_first_sync_pulls_new_remote_file(tmp_path):
    client, server = build_client()
    local = tmp_path / "local"
    local.mkdir()
    server.files["work/data/b.txt"] = bytearray(b"remote content")

    plan = sync_plan(client, str(local), "work/data", state_path=_state(tmp_path))
    entry = plan["entries"][0]
    assert entry["action"] == "pull" and entry["reason"] == "remote-new"

    result = sync_apply(client, plan)
    assert result["moved"]["pulled"] == 1
    assert (local / "b.txt").read_bytes() == b"remote content"


# --------------------------------------------------------------- convergence

def test_second_sync_is_a_no_op_and_moves_nothing(tmp_path):
    client, server = build_client()
    local = tmp_path / "local"
    local.mkdir()
    _write(local / "a.txt", "hello")
    state = _state(tmp_path)

    sync_apply(client, sync_plan(client, str(local), "work/data", state_path=state))

    plan2 = sync_plan(client, str(local), "work/data", state_path=state)
    assert plan2["totals"]["push"] == 0
    assert plan2["totals"]["pull"] == 0
    assert all(e["action"] == "skip" for e in plan2["entries"])

    result2 = sync_apply(client, plan2)
    assert result2["bytes_up"] == 0 and result2["bytes_down"] == 0
    assert result2["moved"]["pushed"] == 0 and result2["moved"]["pulled"] == 0


def test_identical_content_on_both_sides_never_transfers(tmp_path):
    client, server = build_client()
    local = tmp_path / "local"
    local.mkdir()
    _write(local / "same.txt", "identical bytes")
    server.files["work/data/same.txt"] = bytearray(b"identical bytes")
    server.touch("work/data/same.txt")

    plan = sync_plan(client, str(local), "work/data", state_path=_state(tmp_path))
    entry = plan["entries"][0]
    assert entry["action"] == "converge" and entry["reason"] == "identical"

    result = sync_apply(client, plan)
    assert result["moved"]["converged"] == 1
    assert result["bytes_up"] == 0 and result["bytes_down"] == 0


def test_convergence_is_stable_on_a_third_run(tmp_path):
    client, server = build_client()
    local = tmp_path / "local"
    local.mkdir()
    _write(local / "a.txt", "hello")
    state = _state(tmp_path)

    for _ in range(3):
        result = sync_apply(client, sync_plan(client, str(local), "work/data",
                                              state_path=state))
    assert result["moved"]["pushed"] == 0
    assert bytes(server.files["work/data/a.txt"]) == b"hello"


# ---------------------------------------------------------------- deletions

def test_default_delete_policy_deletes_nothing(tmp_path):
    client, server = build_client()
    local = tmp_path / "local"
    local.mkdir()
    _write(local / "a.txt", "hello")
    state = _state(tmp_path)
    sync_apply(client, sync_plan(client, str(local), "work/data", state_path=state))

    os.remove(str(local / "a.txt"))
    plan = sync_plan(client, str(local), "work/data", state_path=state)
    entry = plan["entries"][0]
    assert entry["action"] == "skip" and entry["reason"] == "local-missing"
    result = sync_apply(client, plan)
    assert result["moved"]["deleted_remote"] == 0
    assert "work/data/a.txt" in server.files


def test_push_delete_policy_propagates_local_deletion(tmp_path):
    client, server = build_client()
    local = tmp_path / "local"
    local.mkdir()
    _write(local / "a.txt", "hello")
    state = _state(tmp_path)
    sync_apply(client, sync_plan(client, str(local), "work/data", state_path=state))

    os.remove(str(local / "a.txt"))
    plan = sync_plan(client, str(local), "work/data", state_path=state, delete="push")
    assert plan["entries"][0]["action"] == "push-delete"
    result = sync_apply(client, plan)
    assert result["moved"]["deleted_remote"] == 1
    assert "work/data/a.txt" not in server.files


def test_pull_delete_policy_propagates_remote_deletion(tmp_path):
    client, server = build_client()
    local = tmp_path / "local"
    local.mkdir()
    _write(local / "a.txt", "hello")
    state = _state(tmp_path)
    sync_apply(client, sync_plan(client, str(local), "work/data", state_path=state))

    del server.files["work/data/a.txt"]
    plan = sync_plan(client, str(local), "work/data", state_path=state, delete="pull")
    assert plan["entries"][0]["action"] == "pull-delete"
    result = sync_apply(client, plan)
    assert result["moved"]["deleted_local"] == 1
    assert not (local / "a.txt").exists()


def test_forgotten_path_is_dropped_from_the_baseline(tmp_path):
    client, server = build_client()
    local = tmp_path / "local"
    local.mkdir()
    _write(local / "a.txt", "hello")
    state = _state(tmp_path)
    sync_apply(client, sync_plan(client, str(local), "work/data", state_path=state))

    # Gone from both sides at once (e.g. deleted locally, then that same
    # deletion was independently made on the server too).
    os.remove(str(local / "a.txt"))
    del server.files["work/data/a.txt"]

    plan = sync_plan(client, str(local), "work/data", state_path=state)
    forget_entries = [e for e in plan["entries"] if e["action"] == "forget"]
    assert len(forget_entries) == 1
    result = sync_apply(client, plan)
    assert result["ok"]

    baseline, _ = _load_baseline(state)
    assert baseline["entries"] == {}


def test_first_sync_never_deletes_even_under_both(tmp_path):
    client, server = build_client()
    local = tmp_path / "local"
    local.mkdir()
    # No baseline at all: an empty local dir against an existing remote file
    # must pull it, never delete it, no matter the delete policy.
    server.files["work/data/only_remote.txt"] = bytearray(b"x")

    plan = sync_plan(client, str(local), "work/data", state_path=_state(tmp_path),
                     delete="both")
    assert plan["entries"][0]["action"] == "pull"
    assert plan["totals"]["push_delete"] == 0
    assert plan["totals"]["pull_delete"] == 0


def test_max_deletes_refuses_before_any_request(tmp_path):
    client, server = build_client()
    local = tmp_path / "local"
    local.mkdir()
    state = _state(tmp_path)
    for i in range(30):
        _write(local / "f{}.txt".format(i), "content {}".format(i))
    sync_apply(client, sync_plan(client, str(local), "work/data", state_path=state))

    for i in range(30):
        os.remove(str(local / "f{}.txt".format(i)))

    with pytest.raises(SyncRefused) as excinfo:
        sync_plan(client, str(local), "work/data", state_path=state, delete="push")
    assert excinfo.value.reason == "too-many-deletes"
    assert excinfo.value.to_json()["count"] == 30
    # Nothing was touched on the server.
    assert len(server.files) == 30


def test_max_deletes_override_permits_the_deletion(tmp_path):
    client, server = build_client()
    local = tmp_path / "local"
    local.mkdir()
    state = _state(tmp_path)
    for i in range(30):
        _write(local / "f{}.txt".format(i), "content {}".format(i))
    sync_apply(client, sync_plan(client, str(local), "work/data", state_path=state))
    for i in range(30):
        os.remove(str(local / "f{}.txt".format(i)))

    plan = sync_plan(client, str(local), "work/data", state_path=state,
                     delete="push", max_deletes=100)
    result = sync_apply(client, plan)
    assert result["moved"]["deleted_remote"] == 30
    assert server.files == {}


# ----------------------------------------------------------------- conflicts

def _make_conflict(tmp_path, client, server, *, remote_delay=30.0):
    """A baseline agreement, then both sides edit — local first, remote
    ``remote_delay`` seconds after that (comfortably past
    ``SKEW_TOLERANCE`` by default, so "newest" has an unambiguous answer;
    pass a small ``remote_delay`` to test the "too close to call" case).
    """
    local = tmp_path / "local"
    local.mkdir()
    state = _state(tmp_path)
    now = time.time()
    _write(local / "notes.md", "original")
    os.utime(str(local / "notes.md"), (now, now))
    server.files["work/data/notes.md"] = bytearray(b"original")
    server.set_mtime("work/data/notes.md", now)
    sync_apply(client, sync_plan(client, str(local), "work/data",
                                 state_path=state))

    _write(local / "notes.md", "local edit")
    os.utime(str(local / "notes.md"), (now + 10, now + 10))
    server.files["work/data/notes.md"] = bytearray(b"remote edit, longer")
    server.set_mtime("work/data/notes.md", now + 10 + remote_delay)
    return local, state


def test_ask_policy_leaves_conflict_unresolved(tmp_path):
    client, server = build_client()
    local, state = _make_conflict(tmp_path, client, server)
    plan = sync_plan(client, str(local), "work/data", state_path=state,
                     conflict="ask")
    entry = plan["entries"][0]
    assert entry["action"] == "conflict"
    assert entry["resolution"] is None
    result = sync_apply(client, plan)
    assert result["ok"] is False
    assert result["conflicts_unresolved"] == 1
    assert bytes(server.files["work/data/notes.md"]) == b"remote edit, longer"


def test_skip_policy_leaves_both_sides_untouched(tmp_path):
    client, server = build_client()
    local, state = _make_conflict(tmp_path, client, server)
    plan = sync_plan(client, str(local), "work/data", state_path=state,
                     conflict="skip")
    entry = plan["entries"][0]
    assert entry["action"] == "conflict"
    assert entry["resolution"] == "skip"
    result = sync_apply(client, plan)
    assert result["conflicts_unresolved"] == 1
    assert (local / "notes.md").read_bytes() == b"local edit"
    assert bytes(server.files["work/data/notes.md"]) == b"remote edit, longer"


def test_local_policy_pushes_and_keeps_a_conflict_copy_of_the_remote_loser(
        tmp_path):
    client, server = build_client()
    local, state = _make_conflict(tmp_path, client, server)
    plan = sync_plan(client, str(local), "work/data", state_path=state,
                     conflict="local")
    assert plan["entries"][0]["action"] == "push"
    result = sync_apply(client, plan)
    assert result["ok"]
    assert bytes(server.files["work/data/notes.md"]) == b"local edit"
    assert len(result["conflict_copies"]) == 1
    copy_path = result["conflict_copies"][0]
    assert copy_path.startswith("work/data/notes.md.jsonyter-conflict-")
    assert bytes(server.files[copy_path]) == b"remote edit, longer"


def test_remote_policy_pulls_and_keeps_a_conflict_copy_of_the_local_loser(
        tmp_path):
    client, server = build_client()
    local, state = _make_conflict(tmp_path, client, server)
    plan = sync_plan(client, str(local), "work/data", state_path=state,
                     conflict="remote")
    assert plan["entries"][0]["action"] == "pull"
    result = sync_apply(client, plan)
    assert result["ok"]
    assert (local / "notes.md").read_bytes() == b"remote edit, longer"
    assert len(result["conflict_copies"]) == 1
    copy_path = result["conflict_copies"][0]
    assert copy_path.startswith(str(local / "notes.md")) and \
        ".jsonyter-conflict-" in copy_path
    with open(copy_path, "rb") as handle:
        assert handle.read() == b"local edit"


def test_newest_policy_picks_the_later_side(tmp_path):
    client, server = build_client()
    # _make_conflict's default remote_delay (30s) puts the remote edit
    # comfortably after the local one.
    local, state = _make_conflict(tmp_path, client, server)
    plan = sync_plan(client, str(local), "work/data", state_path=state,
                     conflict="newest")
    entry = plan["entries"][0]
    assert entry["newest"] == "remote"
    assert entry["action"] == "pull"
    sync_apply(client, plan)
    assert (local / "notes.md").read_bytes() == b"remote edit, longer"


def test_newest_degrades_to_unresolved_within_skew_tolerance(tmp_path):
    client, server = build_client()
    # Both sides edited within 2s of each other — well inside the default
    # 5s SKEW_TOLERANCE — so "newest" must refuse to guess.
    local, state = _make_conflict(tmp_path, client, server, remote_delay=2.0)

    plan = sync_plan(client, str(local), "work/data", state_path=state,
                     conflict="newest")
    entry = plan["entries"][0]
    assert entry["action"] == "conflict"
    assert entry["resolution"] is None
    assert any("too close to call" in w for w in plan["warnings"])


def test_conflict_copy_is_excluded_from_the_next_sync(tmp_path):
    client, server = build_client()
    local, state = _make_conflict(tmp_path, client, server)
    plan = sync_plan(client, str(local), "work/data", state_path=state,
                     conflict="local")
    sync_apply(client, plan)

    plan2 = sync_plan(client, str(local), "work/data", state_path=state)
    paths = [e["path"] for e in plan2["entries"]]
    assert not any(".jsonyter-conflict-" in p for p in paths)


# ---------------------------------------------------------------- baseline

def test_missing_baseline_is_not_an_error(tmp_path):
    client, server = build_client()
    local = tmp_path / "local"
    local.mkdir()
    _write(local / "a.txt", "hello")
    plan = sync_plan(client, str(local), "work/data",
                     state_path=str(tmp_path / "does-not-exist.json"))
    assert plan["baseline"] == "absent"
    assert plan["warnings"] == []


def test_corrupt_baseline_is_discarded_with_a_warning(tmp_path):
    client, server = build_client()
    local = tmp_path / "local"
    local.mkdir()
    _write(local / "a.txt", "hello")
    state = _state(tmp_path)
    with open(state, "w") as handle:
        handle.write("not json{{{")

    plan = sync_plan(client, str(local), "work/data", state_path=state)
    assert plan["baseline"] == "absent"
    assert any("could not be read" in w for w in plan["warnings"])
    # Discarding it and re-syncing must still work fine.
    result = sync_apply(client, plan)
    assert result["ok"]


def test_baseline_records_only_files_that_actually_completed(tmp_path,
                                                              monkeypatch):
    client, server = build_client()
    local = tmp_path / "local"
    local.mkdir()
    for i in range(4):
        _write(local / "f{}.txt".format(i), "content {}".format(i))
    state = _state(tmp_path)

    plan = sync_plan(client, str(local), "work/data", state_path=state)
    assert len(plan["entries"]) == 4

    import jsonyter.transfer as transfer_mod
    real_upload = transfer_mod.upload
    calls = {"n": 0}

    def flaky_upload(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 3:
            raise JupyterError("simulated failure")
        return real_upload(*args, **kwargs)

    monkeypatch.setattr(transfer_mod, "upload", flaky_upload)
    result = sync_apply(client, plan)
    assert len(result["failed"]) == 1
    assert result["moved"]["pushed"] == 3

    baseline, _ = _load_baseline(state)
    assert len(baseline["entries"]) == 3
    assert not os.path.exists(state + ".tmp") and not any(
        f.startswith("baseline.json.tmp") for f in os.listdir(tmp_path))

    # Re-running picks up exactly the one that failed.
    plan2 = sync_plan(client, str(local), "work/data", state_path=state)
    assert plan2["totals"]["push"] == 1
    assert plan2["totals"]["skip"] == 3
    result2 = sync_apply(client, plan2)
    assert result2["ok"]


# ------------------------------------------------------------- hash algorithm

def test_pre_2_11_server_degrades_to_size_integrity(tmp_path):
    client, server = build_client()
    server.no_hash = True
    local = tmp_path / "local"
    local.mkdir()
    _write(local / "a.txt", "hello")
    server.files["work/data/b.txt"] = bytearray(b"remote content")

    plan = sync_plan(client, str(local), "work/data", state_path=_state(tmp_path))
    assert plan["integrity"] == "size"
    result = sync_apply(client, plan)
    assert result["integrity"] == "size"
    assert result["ok"]


def test_server_using_sha1_is_compared_in_sha1(tmp_path):
    client, server = build_client()
    server.hash_algorithm = "sha1"
    local = tmp_path / "local"
    local.mkdir()
    _write(local / "a.txt", "same content")
    server.files["work/data/a.txt"] = bytearray(b"same content")
    server.touch("work/data/a.txt")

    plan = sync_plan(client, str(local), "work/data", state_path=_state(tmp_path))
    assert plan["hash_algorithm"] == "sha1"
    assert plan["entries"][0]["action"] == "converge"


def test_local_digest_rejects_unknown_algorithm(tmp_path):
    integrity = _Integrity()
    integrity.algo = "bogus-algo"
    f = tmp_path / "x.bin"
    _write(f, b"hi")
    with pytest.raises(SyncRefused) as excinfo:
        integrity.local_digest(str(f))
    assert excinfo.value.reason == "algorithm-mismatch"


def test_note_model_raises_on_algorithm_change_mid_sync():
    integrity = _Integrity()
    integrity.note_model({"hash": "a" * 40, "hash_algorithm": "sha1"})
    with pytest.raises(SyncRefused) as excinfo:
        integrity.note_model({"hash": "b" * 64, "hash_algorithm": "sha256"})
    assert excinfo.value.reason == "algorithm-mismatch"


# ------------------------------------------------------------------- ignores

def test_git_directory_is_pruned_not_walked(tmp_path):
    client, server = build_client()
    local = tmp_path / "local"
    local.mkdir()
    (local / ".git").mkdir()
    _write(local / ".git" / "config", "should never be seen")
    _write(local / "real.txt", "keep me")

    plan = sync_plan(client, str(local), "work/data", state_path=_state(tmp_path))
    paths = [e["path"] for e in plan["entries"]]
    assert paths == ["real.txt"]


def test_jsonyterignore_file_is_read(tmp_path):
    client, server = build_client()
    local = tmp_path / "local"
    local.mkdir()
    _write(local / ".jsonyterignore", "*.log\n# a comment\nsecret.txt\n")
    _write(local / "keep.txt", "keep")
    _write(local / "debug.log", "noisy")
    _write(local / "secret.txt", "shh")

    plan = sync_plan(client, str(local), "work/data", state_path=_state(tmp_path))
    paths = sorted(e["path"] for e in plan["entries"])
    assert paths == ["keep.txt"]


def test_symlink_is_skipped_and_reported(tmp_path):
    client, server = build_client()
    local = tmp_path / "local"
    local.mkdir()
    target = tmp_path / "outside.txt"
    _write(target, "outside the tree")
    os.symlink(str(target), str(local / "link.txt"))

    plan = sync_plan(client, str(local), "work/data", state_path=_state(tmp_path))
    assert plan["entries"] == []
    assert any("symlink" in w for w in plan["warnings"])


def test_ignored_remote_directory_is_never_listed(tmp_path):
    client, server = build_client()
    local = tmp_path / "local"
    local.mkdir()
    server.dirs.add("work/data/.ipynb_checkpoints")
    server.files["work/data/.ipynb_checkpoints/nb-checkpoint.ipynb"] = \
        bytearray(b"x")
    server.files["work/data/keep.txt"] = bytearray(b"keep")

    plan = sync_plan(client, str(local), "work/data", state_path=_state(tmp_path))
    paths = [e["path"] for e in plan["entries"]]
    assert paths == ["keep.txt"]
    listed = [r[1] for r in client._http.requests if r[0] == "GET"]
    assert not any("checkpoints" in p for p in listed)


# --------------------------------------------------------------- cancellation

def test_cancellation_stops_between_files_and_baseline_allows_resume(tmp_path):
    client, server = build_client()
    local = tmp_path / "local"
    local.mkdir()
    for i in range(5):
        _write(local / "f{}.txt".format(i), "content {}".format(i))
    state = _state(tmp_path)
    plan = sync_plan(client, str(local), "work/data", state_path=state)

    counter = {"n": 0}

    def should_cancel():
        counter["n"] += 1
        return counter["n"] > 2

    result = sync_apply(client, plan, should_cancel=should_cancel)
    assert result.get("cancelled") is True
    assert result["moved"]["pushed"] == 2

    plan2 = sync_plan(client, str(local), "work/data", state_path=state)
    assert plan2["totals"]["skip"] == 2      # the 2 that already landed
    assert plan2["totals"]["push"] == 3      # the 3 the cancel cut off before
    result2 = sync_apply(client, plan2)
    assert result2["ok"]
    assert len(server.files) == 5


# ------------------------------------------------------------------- locking

def test_concurrent_sync_apply_on_the_same_pair_is_refused(tmp_path):
    client, server = build_client()
    local = tmp_path / "local"
    local.mkdir()
    _write(local / "a.txt", "hello")
    state = _state(tmp_path)
    lock_path = state + ".lock"
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with open(lock_path, "w") as handle:
        handle.write("99999")

    plan = sync_plan(client, str(local), "work/data", state_path=state)
    with pytest.raises(SyncRefused) as excinfo:
        sync_apply(client, plan)
    assert excinfo.value.reason == "locked"
    os.remove(lock_path)


def test_stale_lock_is_broken(tmp_path):
    client, server = build_client()
    local = tmp_path / "local"
    local.mkdir()
    _write(local / "a.txt", "hello")
    state = _state(tmp_path)
    lock_path = state + ".lock"
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with open(lock_path, "w") as handle:
        handle.write("99999")
    old = time.time() - 25 * 3600
    os.utime(lock_path, (old, old))

    plan = sync_plan(client, str(local), "work/data", state_path=state)
    result = sync_apply(client, plan)
    assert result["ok"]


# --------------------------------------------------------- request economy

def test_unchanged_tree_costs_one_request_per_directory(tmp_path):
    client, server = build_client()
    local = tmp_path / "local"
    local.mkdir()
    state = _state(tmp_path)
    (local / "sub").mkdir()
    for i in range(5):
        _write(local / "f{}.txt".format(i), "content {}".format(i))
    for i in range(5):
        _write(local / "sub" / "g{}.txt".format(i), "sub content {}".format(i))
    sync_apply(client, sync_plan(client, str(local), "work/data", state_path=state))

    plan2 = sync_plan(client, str(local), "work/data", state_path=state)
    assert plan2["totals"]["skip"] == 10
    # Two directories (the root and "sub"): two list_contents calls, and
    # nothing else, since every file's baseline metadata still matches.
    assert plan2["scanned"]["requests"] == 2


def test_clock_skew_is_measured_and_flips_a_close_newest_call(tmp_path):
    client, server = build_client()
    local = tmp_path / "local"
    local.mkdir()
    state = _state(tmp_path)
    _write(local / "n.md", "original")
    server.files["work/data/n.md"] = bytearray(b"original")
    server.touch("work/data/n.md")
    sync_apply(client, sync_plan(client, str(local), "work/data", state_path=state))

    # The server's clock reads far ahead; without correcting for that, the
    # (only marginally later) local edit would look older than it is.
    _write(local / "n.md", "local edit")
    server.files["work/data/n.md"] = bytearray(b"remote edit")
    server.touch("work/data/n.md")
    future = time.gmtime(time.time() + 3600)
    server.date_header = time.strftime("%a, %d %b %Y %H:%M:%S GMT", future)

    plan = sync_plan(client, str(local), "work/data", state_path=state,
                     conflict="newest")
    assert plan["clock_skew"] is not None
    assert abs(plan["clock_skew"] - 3600) < 5
    assert any("clock skew" in w for w in plan["warnings"])


# ------------------------------------------------------------------- sync()

def test_sync_convenience_defaults_to_newest_and_applies_in_one_call(tmp_path):
    client, server = build_client()
    local = tmp_path / "local"
    local.mkdir()
    _write(local / "a.txt", "hello")
    result = sync(client, str(local), "work/data", state_path=_state(tmp_path))
    assert result["ok"]
    assert bytes(server.files["work/data/a.txt"]) == b"hello"


def test_local_dir_must_exist(tmp_path):
    client, server = build_client()
    with pytest.raises(JupyterError):
        sync_plan(client, str(tmp_path / "nope"), "work/data")


def test_sync_convenience_passes_timeout_to_both_phases(tmp_path, monkeypatch):
    client, server = build_client()
    local = tmp_path / "local"
    local.mkdir()
    _write(local / "a.txt", "hello")

    import sys
    # ``jsonyter/__init__.py`` re-exports the ``sync`` *function* from this
    # same submodule, which shadows the submodule as an attribute of the
    # ``jsonyter`` package — so neither ``jsonyter.sync`` nor
    # ``import jsonyter.sync as x`` reaches the module itself. Only the
    # module cache does.
    sync_mod = sys.modules["jsonyter.sync"]
    seen = {}
    real_plan = sync_mod.sync_plan

    def spy_plan(client, local_dir, remote_dir, **kwargs):
        seen["plan_timeout"] = kwargs.get("timeout")
        return real_plan(client, local_dir, remote_dir, **kwargs)

    monkeypatch.setattr(sync_mod, "sync_plan", spy_plan)
    sync_mod.sync(client, str(local), "work/data",
                 state_path=_state(tmp_path), timeout=12.5)
    assert seen["plan_timeout"] == 12.5
