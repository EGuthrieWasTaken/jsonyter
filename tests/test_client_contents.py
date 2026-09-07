"""The thin Contents API verbs on ``Client``."""

import base64

import pytest

from jsonyter import JupyterError
from conftest import build_client


def test_get_contents_builds_params():
    client, server = build_client()
    server.files["a/b.csv"] = bytearray(b"hello")
    client.get_contents("a/b.csv", content=False, hash=True, format="base64",
                        type="file")
    method, path, query, _body, _headers = server._http_last = \
        client._http.requests[-1]
    assert method == "GET"
    assert path == "/api/contents/a/b.csv"
    assert query["content"] == "0"
    assert query["hash"] == "1"
    assert query["format"] == "base64"
    assert query["type"] == "file"


def test_get_contents_defaults_send_no_optional_params():
    client, server = build_client()
    server.files["a"] = bytearray(b"x")
    client.get_contents("a")
    query = client._http.requests[-1][2]
    assert query == {"content": "1"}
    assert "hash" not in query and "type" not in query and "format" not in query


def test_get_contents_leading_slash_stripped():
    client, server = build_client()
    server.files["x"] = bytearray(b"1")
    client.get_contents("/x")
    assert client._http.requests[-1][1] == "/api/contents/x"


def test_put_contents_omits_chunk_when_none():
    client, server = build_client()
    client.put_contents("d/f.bin", base64.b64encode(b"xy").decode())
    body = client._http.requests[-1][3]
    assert body["type"] == "file"
    assert body["format"] == "base64"
    assert "chunk" not in body
    assert bytes(server.files["d/f.bin"]) == b"xy"


def test_put_contents_chunk_protocol_appends():
    client, server = build_client()
    client.put_contents("f", base64.b64encode(b"AAA").decode(), chunk=1)
    client.put_contents("f", base64.b64encode(b"BBB").decode(), chunk=2)
    client.put_contents("f", base64.b64encode(b"CCC").decode(), chunk=-1)
    assert bytes(server.files["f"]) == b"AAABBBCCC"
    assert client._http.requests[-1][3]["chunk"] == -1


def test_make_directory_uses_put_with_type_directory():
    client, server = build_client()
    client.make_directory("newdir")
    method, path, _q, body, _h = client._http.requests[-1]
    assert method == "PUT"
    assert path == "/api/contents/newdir"
    assert body == {"type": "directory"}
    assert "newdir" in server.dirs


def test_delete_contents_returns_marker():
    client, server = build_client()
    server.files["gone.txt"] = bytearray(b"x")
    result = client.delete_contents("gone.txt")
    assert result == {"path": "gone.txt", "deleted": True}
    assert "gone.txt" not in server.files
    assert client._http.requests[-1][0] == "DELETE"


def test_rename_contents_old_in_url_new_in_body():
    client, server = build_client()
    server.files["old/name.csv"] = bytearray(b"data")
    client.rename_contents("old/name.csv", "new/name.csv")
    method, path, _q, body, _h = client._http.requests[-1]
    assert method == "PATCH"
    assert path == "/api/contents/old/name.csv"
    assert body == {"path": "new/name.csv"}
    assert "new/name.csv" in server.files
    assert "old/name.csv" not in server.files


def test_copy_contents_reads_server_chosen_name():
    client, server = build_client()
    server.files["report.csv"] = bytearray(b"1,2,3")
    model = client.copy_contents("report.csv", "backups")
    method, path, _q, body, _h = client._http.requests[-1]
    assert method == "POST"
    assert path == "/api/contents/backups"
    assert body == {"copy_from": "report.csv"}
    assert model["name"] == "report-Copy1.csv"
    assert model["path"] == "backups/report-Copy1.csv"


def test_list_contents_returns_children():
    client, server = build_client()
    server.dirs.add("proj")
    server.files["proj/a.txt"] = bytearray(b"a")
    server.files["proj/b.txt"] = bytearray(b"b")
    listing = client.list_contents("proj")
    names = sorted(c["name"] for c in listing["content"])
    assert names == ["a.txt", "b.txt"]


def test_proxy_error_is_flagged_with_cf_ray():
    client, server = build_client()
    server.force = (413, {"message": "Request Entity Too Large"},
                    {"cf-ray": "8abc-DFW", "server": "cloudflare"})
    with pytest.raises(JupyterError) as excinfo:
        client.put_contents("big", "")
    err = excinfo.value
    assert err.status == 413
    assert err.cf_ray == "8abc-DFW"
    assert "proxy" in err.message
    assert err.to_json()["cf_ray"] == "8abc-DFW"


def test_non_proxy_error_has_no_cf_ray_in_json():
    client, server = build_client()
    with pytest.raises(JupyterError) as excinfo:
        client.get_contents("missing")
    assert excinfo.value.status == 404
    assert "cf_ray" not in excinfo.value.to_json()
