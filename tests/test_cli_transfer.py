"""The stdio bridge's wiring for transfer: dispatch, progress lines, cache."""

import io
import json

import pytest

from jsonyter.cli import Dispatcher, _CLIENT_METHODS
from conftest import FakeConn, build_client

CS = 64


def make_dispatcher(client, **kw):
    return Dispatcher(client, stdin=io.StringIO(""), stdout=io.StringIO(), **kw)


def emitted(dispatcher):
    lines = [l for l in dispatcher.stdout.getvalue().splitlines() if l.strip()]
    return [json.loads(l) for l in lines]


def test_transfer_methods_are_on_the_rest_pool_not_kernel_methods():
    from jsonyter.cli import _KERNEL_METHODS
    for name in ("upload", "download", "get_contents", "put_contents",
                 "kernel_contents_dir"):
        assert name in _CLIENT_METHODS
        assert name not in _KERNEL_METHODS


def test_dispatch_upload_emits_progress_then_result(tmp_path):
    client, server = build_client()
    src = tmp_path / "s.bin"
    src.write_bytes(b"z" * (3 * CS + 10))
    d = make_dispatcher(client)

    reply = d.dispatch({"id": 5, "method": "upload", "params": {
        "local_path": str(src), "remote_path": "data/x.bin", "chunk_size": CS}})

    assert reply["id"] == 5
    assert reply["result"]["verified"] == "sha256"
    lines = emitted(d)
    progress = [l for l in lines if "progress" in l]
    assert progress, "expected progress lines on stdout"
    assert all(l["id"] == 5 for l in progress)
    assert progress[-1]["progress"]["bytes_done"] == 3 * CS + 10
    assert bytes(server.files["data/x.bin"]) == b"z" * (3 * CS + 10)


def test_dispatch_upload_injects_default_chunk_size(tmp_path):
    client, server = build_client()
    src = tmp_path / "s.bin"
    src.write_bytes(b"y" * 100)
    d = make_dispatcher(client, chunk_size=32)
    d.dispatch({"id": 1, "method": "upload", "params": {
        "local_path": str(src), "remote_path": "x"}})
    # 100 bytes / 32 -> 4 PUTs (1, 2, 3, -1)
    chunks = [r[3].get("chunk") for r in client._http.requests
              if r[0] == "PUT"]
    assert chunks == [1, 2, 3, -1]


def test_progress_emitter_rate_limits_but_never_drops_final():
    client, _ = build_client()
    d = make_dispatcher(client)

    slow = d._progress_emitter(7, min_interval=10 ** 9)
    slow({"bytes_done": 1, "bytes_total": 9})
    slow({"bytes_done": 4, "bytes_total": 9})
    slow({"bytes_done": 9, "bytes_total": 9})     # final -> always through
    lines = emitted(d)
    assert len(lines) == 1
    assert lines[0]["progress"]["bytes_done"] == 9

    d.stdout = io.StringIO()
    fast = d._progress_emitter(7, min_interval=0)
    fast({"bytes_done": 1, "bytes_total": 9})
    fast({"bytes_done": 4, "bytes_total": 9})
    fast({"bytes_done": 9, "bytes_total": 9})
    assert len(emitted(d)) == 3


def test_dispatch_conflict_comes_back_as_error(tmp_path):
    client, server = build_client()
    server.files["dest"] = bytearray(b"there")
    src = tmp_path / "s.bin"
    src.write_bytes(b"here")
    d = make_dispatcher(client)

    with pytest.raises(Exception) as excinfo:      # dispatch re-raises
        d.dispatch({"id": 3, "method": "upload", "params": {
            "local_path": str(src), "remote_path": "dest"}})
    payload = excinfo.value.to_json()
    assert payload["reason"] == "exists"
    assert payload["error"] == "TransferConflict"


def test_kernel_contents_dir_is_cached_and_invalidated_on_restart(monkeypatch):
    client, server = build_client()
    d = make_dispatcher(client)

    conn = FakeConn(server, cwd="/srv/work/data", contents_prefix="work/data")
    monkeypatch.setattr(d, "_connection", lambda kid: conn)

    first = d.dispatch({"id": 1, "method": "kernel_contents_dir",
                        "params": {"kernel_id": "k1"}})["result"]
    assert first["method"] == "probe"
    assert first["cached"] is False
    runs_after_first = len(conn.executed)

    second = d.dispatch({"id": 2, "method": "kernel_contents_dir",
                         "params": {"kernel_id": "k1"}})["result"]
    assert second["cached"] is True
    assert len(conn.executed) == runs_after_first        # no re-probe

    d.dispatch({"id": 3, "method": "restart_kernel",
                "params": {"kernel_id": "k1"}})
    assert "k1" not in d._contents_dir_cache

    third = d.dispatch({"id": 4, "method": "kernel_contents_dir",
                        "params": {"kernel_id": "k1"}})["result"]
    assert third["cached"] is False
    assert len(conn.executed) > runs_after_first         # probed again


def test_kernel_contents_dir_requires_kernel_id():
    client, _ = build_client()
    d = make_dispatcher(client)
    with pytest.raises(Exception) as excinfo:
        d.dispatch({"id": 1, "method": "kernel_contents_dir", "params": {}})
    assert "kernel_id" in str(excinfo.value)


def test_methods_listing_includes_the_new_verbs():
    client, _ = build_client()
    d = make_dispatcher(client)
    listed = d.dispatch({"id": 1, "method": "methods"})["result"]
    for name in ("upload", "download", "put_contents", "rename_contents",
                 "copy_contents", "list_contents", "kernel_contents_dir"):
        assert name in listed
