"""Opt-in round-trip against a real ``jupyter server``.

Skipped unless ``JSONYTER_LIVE_URL`` (and usually ``JSONYTER_LIVE_TOKEN``) are
set, e.g.::

    jupyter server --ServerApp.token=secret --port 8888 &
    JSONYTER_LIVE_URL=http://localhost:8888 JSONYTER_LIVE_TOKEN=secret \
        pytest tests/test_integration.py -q
"""

import base64
import hashlib
import os

import pytest

import jsonyter

LIVE_URL = os.environ.get("JSONYTER_LIVE_URL")

pytestmark = pytest.mark.skipif(
    not LIVE_URL, reason="set JSONYTER_LIVE_URL to run the live integration test")


@pytest.fixture
def live_client():
    return jsonyter.Client(
        LIVE_URL, token=os.environ.get("JSONYTER_LIVE_TOKEN") or False,
        verify_tls=os.environ.get("JSONYTER_LIVE_INSECURE") != "1")


def test_live_upload_download_round_trip(live_client, tmp_path):
    data = os.urandom(3 * 1024 * 1024 + 7)
    src = tmp_path / "payload.bin"
    src.write_bytes(data)
    remote = "jsonyter-itest/payload.bin"

    live_client.make_directory("jsonyter-itest")
    try:
        up = jsonyter.upload(live_client, str(src), remote,
                             chunk_size=1024 * 1024, overwrite=True)
        assert up["hash"] == hashlib.sha256(data).hexdigest()
        assert up["verified"] in ("sha256", "size")

        out = tmp_path / "back.bin"
        down = jsonyter.download(live_client, remote, str(out), overwrite=True)
        assert out.read_bytes() == data
        assert down["hash"] == up["hash"]
    finally:
        live_client.delete_contents(remote)
        live_client.delete_contents("jsonyter-itest")


def test_live_sync_round_trips_a_small_tree_in_both_directions(live_client,
                                                                tmp_path):
    remote_dir = "jsonyter-itest-sync"
    local = tmp_path / "local"
    local.mkdir()
    (local / "a.txt").write_bytes(b"pushed from local")
    state_path = str(tmp_path / "state.json")

    live_client.make_directory(remote_dir)
    try:
        result = jsonyter.sync(live_client, str(local), remote_dir,
                               state_path=state_path)
        assert result["ok"]
        assert live_client.get_contents(
            remote_dir + "/a.txt", content=True)["content"] == \
            "pushed from local"

        live_client.put_contents(
            remote_dir + "/b.txt",
            base64.b64encode(b"pulled from remote").decode(),
            format="base64")
        result2 = jsonyter.sync(live_client, str(local), remote_dir,
                                state_path=state_path)
        assert result2["ok"]
        assert (local / "b.txt").read_bytes() == b"pulled from remote"

        # A no-op third sync: nothing left to move.
        result3 = jsonyter.sync(live_client, str(local), remote_dir,
                                state_path=state_path)
        assert result3["bytes_up"] == 0 and result3["bytes_down"] == 0
    finally:
        for name in ("a.txt", "b.txt"):
            try:
                live_client.delete_contents(remote_dir + "/" + name)
            except jsonyter.JupyterError:
                pass
        live_client.delete_contents(remote_dir)
