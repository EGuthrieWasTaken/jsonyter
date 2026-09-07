"""Opt-in round-trip against a real ``jupyter server``.

Skipped unless ``JSONYTER_LIVE_URL`` (and usually ``JSONYTER_LIVE_TOKEN``) are
set, e.g.::

    jupyter server --ServerApp.token=secret --port 8888 &
    JSONYTER_LIVE_URL=http://localhost:8888 JSONYTER_LIVE_TOKEN=secret \
        pytest tests/test_integration.py -q
"""

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
