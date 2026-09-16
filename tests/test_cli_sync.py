"""The stdio bridge's wiring for sync: dispatch, progress lines, cancellation."""

import io
import json

import pytest

from jsonyter.cli import Dispatcher, _CLIENT_METHODS, _KERNEL_METHODS
from conftest import build_client


def make_dispatcher(client, **kw):
    return Dispatcher(client, stdin=io.StringIO(""), stdout=io.StringIO(), **kw)


def emitted(dispatcher):
    lines = [l for l in dispatcher.stdout.getvalue().splitlines() if l.strip()]
    return [json.loads(l) for l in lines]


def test_sync_methods_are_on_the_rest_pool_not_kernel_methods():
    for name in ("sync_plan", "sync_apply", "sync", "sync_status",
                "cancel_sync"):
        assert name in _CLIENT_METHODS
        assert name not in _KERNEL_METHODS


def test_methods_listing_includes_sync_verbs():
    client, _server = build_client()
    d = make_dispatcher(client)
    listed = d.dispatch({"id": 1, "method": "methods"})["result"]
    for name in ("sync_plan", "sync_apply", "sync", "sync_status",
                "cancel_sync"):
        assert name in listed


def test_dispatch_sync_plan_and_apply(tmp_path):
    client, server = build_client()
    local = tmp_path / "local"
    local.mkdir()
    (local / "a.txt").write_bytes(b"hello")
    d = make_dispatcher(client)

    plan_reply = d.dispatch({"id": 1, "method": "sync_plan", "params": {
        "local_dir": str(local), "remote_dir": "work/data",
        "state_path": str(tmp_path / "state.json")}})
    plan = plan_reply["result"]
    assert plan["entries"][0]["action"] == "push"

    apply_reply = d.dispatch({"id": 2, "method": "sync_apply", "params": {
        "plan": plan}})
    result = apply_reply["result"]
    assert result["ok"]
    assert bytes(server.files["work/data/a.txt"]) == b"hello"


def test_dispatch_sync_one_shot(tmp_path):
    client, server = build_client()
    local = tmp_path / "local"
    local.mkdir()
    (local / "a.txt").write_bytes(b"hello")
    d = make_dispatcher(client)

    reply = d.dispatch({"id": 1, "method": "sync", "params": {
        "local_dir": str(local), "remote_dir": "work/data",
        "state_path": str(tmp_path / "state.json")}})
    assert reply["result"]["ok"]
    assert bytes(server.files["work/data/a.txt"]) == b"hello"


def test_dispatch_sync_status_never_writes(tmp_path):
    client, server = build_client()
    local = tmp_path / "local"
    local.mkdir()
    (local / "a.txt").write_bytes(b"hello")
    d = make_dispatcher(client)

    reply = d.dispatch({"id": 1, "method": "sync_status", "params": {
        "local_dir": str(local), "remote_dir": "work/data",
        "state_path": str(tmp_path / "state.json")}})
    assert reply["result"]["entries"][0]["action"] == "push"
    assert server.files == {}


def test_sync_apply_emits_sync_phase_progress(tmp_path):
    client, server = build_client()
    local = tmp_path / "local"
    local.mkdir()
    (local / "a.txt").write_bytes(b"z" * 500)
    d = make_dispatcher(client, chunk_size=64)

    plan = d.dispatch({"id": 1, "method": "sync_plan", "params": {
        "local_dir": str(local), "remote_dir": "work/data",
        "state_path": str(tmp_path / "state.json")}})["result"]
    d.dispatch({"id": 2, "method": "sync_apply", "params": {"plan": plan}})

    lines = emitted(d)
    progress = [l["progress"] for l in lines if "progress" in l]
    assert progress, "expected progress lines"
    sync_lines = [p for p in progress if p.get("phase") == "sync"]
    assert sync_lines
    assert all(p["op"] == "push" for p in sync_lines)
    assert all("files_total" in p and "sync_bytes_total" in p
              for p in sync_lines)
    assert sync_lines[-1]["bytes_done"] == sync_lines[-1]["bytes_total"]


def test_cancel_sync_stops_an_in_flight_apply(tmp_path):
    client, server = build_client()
    local = tmp_path / "local"
    local.mkdir()
    for i in range(5):
        (local / "f{}.txt".format(i)).write_bytes(
            "content {}".format(i).encode())
    d = make_dispatcher(client)

    plan = d.dispatch({"id": 1, "method": "sync_plan", "params": {
        "local_dir": str(local), "remote_dir": "work/data",
        "state_path": str(tmp_path / "state.json")}})["result"]

    d.dispatch({"id": 2, "method": "cancel_sync", "params": {"request_id": 3}})
    result = d.dispatch({"id": 3, "method": "sync_apply",
                         "params": {"plan": plan}})["result"]
    assert result.get("cancelled") is True
    assert result["moved"]["pushed"] == 0
    # The flag is consumed — a later sync_apply under the same id must not
    # be cancelled before it even starts.
    assert 3 not in d._cancelled


def test_dispatch_sync_conflict_comes_back_as_error(tmp_path):
    client, server = build_client()
    local = tmp_path / "local"
    local.mkdir()
    (local / "nope").mkdir()
    d = make_dispatcher(client)

    with pytest.raises(Exception) as excinfo:
        d.dispatch({"id": 1, "method": "sync_plan", "params": {
            "local_dir": str(tmp_path / "missing"), "remote_dir": "work/data"}})
    assert "no such local directory" in str(excinfo.value)
