"""``jsonyter.transfer.upload`` / ``download`` round-trips and edge cases."""

import hashlib
import os

import pytest

from jsonyter import JupyterError
from jsonyter.transfer import (MAX_SAFE_CHUNK_SIZE, TransferConflict, download,
                               upload)
from conftest import build_client

CS = 64  # tiny chunk so the size matrix stays cheap


def _rand(n):
    # Deterministic pseudo-random bytes (no Math.random equivalent needed).
    out = bytearray()
    x = 0x9E3779B9 ^ n
    for _ in range(n):
        x = (1103515245 * x + 12345) & 0xFFFFFFFF
        out.append((x >> 16) & 0xFF)
    return bytes(out)


@pytest.mark.parametrize("size", [0, 1, CS, CS + 1, 3 * CS + 17])
def test_round_trip_byte_identity(tmp_path, size):
    client, server = build_client()
    data = _rand(size)
    src = tmp_path / "src.bin"
    src.write_bytes(data)

    up = upload(client, str(src), "data/trials.csv", chunk_size=CS)
    assert up["bytes"] == size
    assert up["hash"] == hashlib.sha256(data).hexdigest()
    assert up["verified"] == "sha256"
    assert bytes(server.files["data/trials.csv"]) == data

    dst = tmp_path / "out.bin"
    down = download(client, "data/trials.csv", str(dst))
    assert dst.read_bytes() == data
    assert down["hash"] == up["hash"]
    assert down["verified"] == "sha256"
    assert not os.path.exists(str(dst) + ".part")


def test_last_chunk_is_marked_and_single_chunk_has_no_chunk_key(tmp_path):
    client, server = build_client()
    calls = []
    orig = client.put_contents

    def spy(path, content, **kw):
        calls.append(kw.get("chunk"))
        return orig(path, content, **kw)

    client.put_contents = spy

    multi = tmp_path / "multi.bin"
    multi.write_bytes(_rand(3 * CS + 5))
    upload(client, str(multi), "m.bin", chunk_size=CS)
    assert calls == [1, 2, 3, -1]

    calls.clear()
    lone = tmp_path / "lone.bin"
    lone.write_bytes(_rand(CS - 3))
    upload(client, str(lone), "l.bin", chunk_size=CS)
    assert calls == [None]


def test_oversized_chunk_size_rejected_before_any_request(tmp_path):
    client, server = build_client()
    src = tmp_path / "s.bin"
    src.write_bytes(b"x" * 10)
    with pytest.raises(JupyterError) as excinfo:
        upload(client, str(src), "s.bin", chunk_size=MAX_SAFE_CHUNK_SIZE + 1)
    assert "74 MB" in str(excinfo.value)
    assert client._http.requests == []


def test_resume_from_chunk_multiple(tmp_path):
    client, server = build_client()
    data = _rand(3 * CS + 20)
    src = tmp_path / "s.bin"
    src.write_bytes(data)
    # A partial upload landed the first two whole chunks.
    server.files["r.bin"] = bytearray(data[:2 * CS])

    result = upload(client, str(src), "r.bin", chunk_size=CS, resume=True)
    assert result["resumed_at"] == 2 * CS
    assert result["resume_note"] is None
    assert bytes(server.files["r.bin"]) == data
    assert result["verified"] == "sha256"


def test_resume_from_non_multiple_restarts_and_says_so(tmp_path):
    client, server = build_client()
    data = _rand(3 * CS + 20)
    src = tmp_path / "s.bin"
    src.write_bytes(data)
    # A chunk landed partially: size is not a multiple of CS.
    server.files["r.bin"] = bytearray(data[:2 * CS + 5])

    result = upload(client, str(src), "r.bin", chunk_size=CS, resume=True)
    assert result["resumed_at"] == 0
    assert "not a multiple" in result["resume_note"]
    assert bytes(server.files["r.bin"]) == data


def test_conflict_exists_without_overwrite(tmp_path):
    client, server = build_client()
    src = tmp_path / "s.bin"
    src.write_bytes(b"new-bytes")
    server.files["dest.csv"] = bytearray(b"already here")

    with pytest.raises(TransferConflict) as excinfo:
        upload(client, str(src), "dest.csv", chunk_size=CS)
    assert excinfo.value.reason == "exists"
    assert bytes(server.files["dest.csv"]) == b"already here"


def test_conflict_stale_expect_hash(tmp_path):
    client, server = build_client()
    src = tmp_path / "s.bin"
    src.write_bytes(b"payload")
    server.files["dest.csv"] = bytearray(b"server copy")

    with pytest.raises(TransferConflict) as excinfo:
        upload(client, str(src), "dest.csv", chunk_size=CS,
               expect_hash="dead" * 16)
    assert excinfo.value.reason == "stale"
    assert excinfo.value.to_json()["reason"] == "stale"


def test_matching_expect_hash_authorizes_overwrite(tmp_path):
    client, server = build_client()
    old = b"server copy"
    server.files["dest.csv"] = bytearray(old)
    src = tmp_path / "s.bin"
    src.write_bytes(b"fresh contents")

    result = upload(client, str(src), "dest.csv", chunk_size=CS,
                    expect_hash=hashlib.sha256(old).hexdigest())
    assert bytes(server.files["dest.csv"]) == b"fresh contents"
    assert result["verified"] == "sha256"


def test_download_falls_back_when_range_ignored(tmp_path):
    client, server = build_client()
    data = _rand(5 * CS)
    server.files["big.bin"] = bytearray(data)
    server.ignore_range = True

    dst = tmp_path / "out.bin"
    result = download(client, "big.bin", str(dst))
    assert dst.read_bytes() == data
    assert result["transport"] == "files"
    assert "ignored Range" in result["resume_note"]


def test_download_falls_back_to_contents_api(tmp_path):
    client, server = build_client()
    data = _rand(4 * CS + 9)
    server.files["s3/obj.bin"] = bytearray(data)
    server.no_files = True

    dst = tmp_path / "out.bin"
    result = download(client, "s3/obj.bin", str(dst))
    assert dst.read_bytes() == data
    assert result["transport"] == "contents"
    assert result["verified"] == "sha256"


def test_ranged_download_resume(tmp_path):
    client, server = build_client()
    data = _rand(6 * CS)
    server.files["r.bin"] = bytearray(data)

    dst = tmp_path / "out.bin"
    part = str(dst) + ".part"
    with open(part, "wb") as fh:
        fh.write(data[:2 * CS + 7])          # a partial download to continue

    result = download(client, "r.bin", str(dst), resume=True)
    assert dst.read_bytes() == data
    assert result["resumed_at"] == 2 * CS + 7
    assert result["transport"] == "files"


def test_interrupted_download_leaves_only_part_file(tmp_path):
    client, server = build_client()
    server.files["x.bin"] = bytearray(_rand(3 * CS))
    server.corrupt_hash = True                # verification will fail

    dst = tmp_path / "out.bin"
    with pytest.raises(TransferConflict) as excinfo:
        download(client, "x.bin", str(dst))
    assert excinfo.value.reason == "corrupt"
    assert not dst.exists()
    assert os.path.exists(str(dst) + ".part")


def test_download_refuses_existing_local_without_overwrite(tmp_path):
    client, server = build_client()
    server.files["x.bin"] = bytearray(b"remote")
    dst = tmp_path / "out.bin"
    dst.write_bytes(b"local already")
    with pytest.raises(TransferConflict) as excinfo:
        download(client, "x.bin", str(dst))
    assert excinfo.value.reason == "exists"
    assert dst.read_bytes() == b"local already"


def test_download_overwrite_replaces_existing_local(tmp_path):
    client, server = build_client()
    data = _rand(2 * CS + 3)
    server.files["x.bin"] = bytearray(data)
    dst = tmp_path / "out.bin"
    dst.write_bytes(b"stale local copy")

    result = download(client, "x.bin", str(dst), overwrite=True)
    assert dst.read_bytes() == data
    assert result["verified"] == "sha256"
    assert not os.path.exists(str(dst) + ".part")


def test_download_stale_expect_hash(tmp_path):
    client, server = build_client()
    server.files["x.bin"] = bytearray(b"remote bytes")
    dst = tmp_path / "out.bin"
    with pytest.raises(TransferConflict) as excinfo:
        download(client, "x.bin", str(dst), expect_hash="beef" * 16)
    assert excinfo.value.reason == "stale"


def test_old_server_degrades_to_size_verification(tmp_path):
    client, server = build_client()
    server.no_hash = True
    data = _rand(2 * CS + 1)
    src = tmp_path / "s.bin"
    src.write_bytes(data)

    up = upload(client, str(src), "d.bin", chunk_size=CS)
    assert up["verified"] == "size"

    dst = tmp_path / "out.bin"
    down = download(client, "d.bin", str(dst))
    assert down["verified"] == "size"
    assert dst.read_bytes() == data


def test_upload_unverified_when_hash_request_times_out(tmp_path, monkeypatch):
    client, server = build_client()
    data = _rand(2 * CS)
    src = tmp_path / "s.bin"
    src.write_bytes(data)

    real = client.get_contents
    calls = {"n": 0}

    def flaky(path, **kw):
        # First call is the preflight; a later hash=1 call "times out".
        if kw.get("hash"):
            calls["n"] += 1
            raise JupyterError("read timed out", url="x")  # status is None
        return real(path, **kw)

    monkeypatch.setattr(client, "get_contents", flaky)
    result = upload(client, str(src), "d.bin", chunk_size=CS)
    assert result["verified"] == "unverified"
    assert bytes(server.files["d.bin"]) == data


def test_progress_is_monotonic_and_final_hits_total(tmp_path):
    client, server = build_client()
    data = _rand(4 * CS + 3)
    src = tmp_path / "s.bin"
    src.write_bytes(data)
    events = []
    upload(client, str(src), "d.bin", chunk_size=CS, progress=events.append)

    assert events, "expected at least one progress event"
    seen = [e["bytes_done"] for e in events]
    assert seen == sorted(seen)
    assert events[-1]["bytes_done"] == events[-1]["bytes_total"] == len(data)
    assert all(e["phase"] == "upload" for e in events)
    assert events[-1]["chunk"] == events[-1]["chunks_total"]


def test_upload_chunk_failure_names_numbers_and_recovery(tmp_path):
    client, server = build_client()
    src = tmp_path / "s.bin"
    src.write_bytes(_rand(5 * CS))
    # The 3rd PUT (chunk 3) fails with a Cloudflare 413.
    server.fail_put_after = (2, 413, {"cf-ray": "9xy-DFW", "server": "cloudflare"})

    with pytest.raises(JupyterError) as excinfo:
        upload(client, str(src), "d.bin", chunk_size=CS)
    msg = str(excinfo.value)
    assert "chunk 3/5" in msg
    assert "written" in msg
    assert "resume from byte {}".format(2 * CS) in msg
    assert "lower --chunk-size" in msg
    assert "proxy" in msg and "cf-ray" in msg
    assert excinfo.value.status == 413
    assert excinfo.value.cf_ray == "9xy-DFW"


def test_download_range_failure_keeps_part_and_names_recovery(tmp_path,
                                                             monkeypatch):
    import jsonyter.transfer as T
    monkeypatch.setattr(T, "DOWNLOAD_RANGE_SIZE", CS)
    client, server = build_client()
    server.files["x.bin"] = bytearray(_rand(4 * CS))
    # probe 206, first ranged GET 206, then a Cloudflare 503.
    server.range_error_after = (1, 503, {"cf-ray": "5aa", "server": "cloudflare"})

    dst = tmp_path / "out.bin"
    with pytest.raises(JupyterError) as excinfo:
        download(client, "x.bin", str(dst))
    msg = str(excinfo.value)
    assert "re-run with resume" in msg
    assert "proxy" in msg
    assert not dst.exists()
    assert os.path.exists(str(dst) + ".part")


def test_ranged_download_rejects_midstream_200(tmp_path, monkeypatch):
    import jsonyter.transfer as T
    monkeypatch.setattr(T, "DOWNLOAD_RANGE_SIZE", CS)
    client, server = build_client()
    data = _rand(4 * CS)
    server.files["x.bin"] = bytearray(data)
    server.range_200_after = 1          # first range 206, then 200 whole-body

    dst = tmp_path / "out.bin"
    with pytest.raises(JupyterError) as excinfo:
        download(client, "x.bin", str(dst))
    assert "without resume" in str(excinfo.value)
    assert not dst.exists()
    assert os.path.exists(str(dst) + ".part")


def test_upload_rejects_directory_destination(tmp_path):
    client, server = build_client()
    server.dirs.add("adir")
    src = tmp_path / "s.bin"
    src.write_bytes(b"x")
    with pytest.raises(JupyterError) as excinfo:
        upload(client, str(src), "adir", chunk_size=CS)
    assert "directory" in str(excinfo.value)


def test_download_rejects_directory_source(tmp_path):
    client, server = build_client()
    server.dirs.add("adir")
    with pytest.raises(JupyterError) as excinfo:
        download(client, "adir", str(tmp_path / "out"))
    assert "directory" in str(excinfo.value)
