"""``jsonyter.transfer.kernel_contents_dir`` — the cwd -> contents-path probe."""

from jsonyter.transfer import kernel_contents_dir
from conftest import FakeConn, build_client


def test_probe_resolves_cwd_to_contents_path():
    client, server = build_client()
    conn = FakeConn(server, cwd="/srv/work/data", contents_prefix="work/data")
    result = kernel_contents_dir(client, "k1", conn=conn)
    assert result["method"] == "probe"
    assert result["contents_dir"] == "work/data"
    assert result["root_dir"] == "/srv"
    assert result["cwd"] == "/srv/work/data"
    # The sentinel is cleaned up afterwards.
    assert not any(k.startswith("work/data/.jsonyter-probe-")
                   for k in server.files)


def test_probe_uses_sentinel_to_disambiguate_two_matching_suffixes():
    client, server = build_client()
    # A decoy directory matches the *longest* suffix but has no sentinel;
    # the real one matches a shorter suffix. Suffix-existence alone would
    # pick the decoy — the sentinel is what forces the right answer.
    server.dirs.add("data/work")
    server.files["data/work/decoy.txt"] = bytearray(b"x")
    conn = FakeConn(server, cwd="/data/work", contents_prefix="work")
    result = kernel_contents_dir(client, "k1", conn=conn)
    assert result["contents_dir"] == "work"
    assert result["root_dir"] == "/data"


def test_probe_returns_null_when_kernel_is_outside_root_dir():
    client, server = build_client()
    conn = FakeConn(server, cwd="/home/user/elsewhere", visible=False)
    result = kernel_contents_dir(client, "k1", conn=conn)
    assert result["method"] == "unresolved"
    assert result["contents_dir"] is None
    assert result["root_dir"] is None


def test_configured_root_skips_the_probe():
    client, server = build_client()
    conn = FakeConn(server, cwd="/srv/work/data")
    result = kernel_contents_dir(client, "k1", root="/srv", conn=conn)
    assert result["method"] == "configured"
    assert result["contents_dir"] == "work/data"
    assert result["root_dir"] == "/srv"
    assert not any("probe-" in c for c in conn.executed)


def test_configured_root_reports_null_when_cwd_not_under_root():
    client, server = build_client()
    conn = FakeConn(server, cwd="/somewhere/else")
    result = kernel_contents_dir(client, "k1", root="/srv", conn=conn)
    assert result["method"] == "configured"
    assert result["contents_dir"] is None


def test_unsupported_language_is_not_an_error():
    client, server = build_client()
    conn = FakeConn(server, language="haskell")
    result = kernel_contents_dir(client, "k1", conn=conn)
    assert result["method"] == "unsupported"
    assert result["contents_dir"] is None
    assert conn.executed == []


def test_cwd_probe_failure_is_unresolved():
    client, server = build_client()
    conn = FakeConn(server, fail_cwd=True)
    result = kernel_contents_dir(client, "k1", conn=conn)
    assert result["method"] == "unresolved"
    assert result["contents_dir"] is None


def test_language_alias_ir_maps_to_r():
    client, server = build_client()
    conn = FakeConn(server, language="ir", cwd="/srv/work/data",
                    contents_prefix="work/data")
    result = kernel_contents_dir(client, "k1", conn=conn)
    assert result["method"] == "probe"
    assert result["contents_dir"] == "work/data"
    # R snippets, not Python ones.
    assert any("getwd()" in c for c in conn.executed)


def test_julia_probe_uses_valid_double_quoted_snippets():
    client, server = build_client()
    conn = FakeConn(server, language="julia", cwd="/srv/work/data",
                    contents_prefix="work/data")
    result = kernel_contents_dir(client, "k1", conn=conn)
    assert result["method"] == "probe"
    assert result["contents_dir"] == "work/data"
    touch = next(c for c in conn.executed if "touch(" in c)
    assert '"' in touch and "'" not in touch          # double-quoted, valid Julia
    assert any("pwd()" in c for c in conn.executed)


def test_sentinel_write_failure_degrades_to_unresolved():
    client, server = build_client()
    conn = FakeConn(server, fail_touch=True)
    result = kernel_contents_dir(client, "k1", conn=conn)
    assert result["method"] == "unresolved"
    assert result["contents_dir"] is None
    # Nothing left behind on the server.
    assert not any("probe-" in k for k in server.files)
