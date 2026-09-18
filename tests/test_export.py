"""Regression tests for the export defects in BUG-REPORT-jsonyter-bridge-export.md.

No live server or kernel needed: ``Client._request_raw`` is monkeypatched to
play back canned responses, the same approach ``test_client_contents.py``
uses for the plain Contents API verbs.
"""

import io
import zipfile

import pytest

from jsonyter import ExportError, JupyterError
from jsonyter.export import (TOOLCHAIN_HINTS, _toolchain_hint, export_notebook,
                             list_export_formats)
from conftest import FakeResponse, build_client


class _FakeRaw:
    """Stand-in for ``Client._request_raw``: plays back canned responses in
    order and records every call it was given."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, path, json_body=None, params=None, timeout=None):
        self.calls.append({"method": method, "path": path,
                           "json_body": json_body, "params": params,
                           "timeout": timeout})
        return self.responses.pop(0)


def _zip_bytes(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _html_error(detail):
    return ('<html><body><pre class="traceback">nbconvert failed: {}</pre>'
           '</body></html>').format(detail)


# --------------------------------------------------------------------- B1
# list_export_formats must return a dict, never raise, for 401 and 403.

def test_list_export_formats_401_does_not_raise(monkeypatch):
    client, _ = build_client()
    fake = _FakeRaw(FakeResponse(401, body="<html><h1>401 Unauthorized</h1></html>",
                                 url="http://jupyter.test/api/nbconvert"))
    monkeypatch.setattr(client, "_request_raw", fake)

    result = list_export_formats(client)

    assert result["available"] is False
    assert result["formats"] == {}
    assert "token" in result["reason"]


def test_list_export_formats_403_does_not_raise(monkeypatch):
    client, _ = build_client()
    fake = _FakeRaw(FakeResponse(403, body="<html><h1>403 Forbidden</h1></html>",
                                 url="http://jupyter.test/api/nbconvert"))
    monkeypatch.setattr(client, "_request_raw", fake)

    result = list_export_formats(client)

    assert result["available"] is False
    assert result["formats"] == {}
    assert "token" in result["reason"]


def test_ambiguous_500_probe_403_no_longer_masks_the_original_error(monkeypatch):
    """Before the fix, a 403 from the follow-up probe raised straight through
    ``_handle_ambiguous_500`` and replaced the real export failure with a
    bare ``HTTP 403 Forbidden``."""
    client, _ = build_client()
    ambiguous_500 = FakeResponse(500, body="<html><body>Internal Server Error</body></html>",
                                 url="http://jupyter.test/nbconvert/somefmt")
    probe_403 = FakeResponse(403, body="<html><h1>403 Forbidden</h1></html>",
                             url="http://jupyter.test/api/nbconvert")
    fake = _FakeRaw(ambiguous_500, probe_403)
    monkeypatch.setattr(client, "_request_raw", fake)

    with pytest.raises(ExportError) as excinfo:
        export_notebook(client, "somefmt",
                        cells=[{"cell_type": "code", "source": "1"}])

    err = excinfo.value
    assert err.status == 500
    assert "403" not in (err.message or "")


# --------------------------------------------------------------------- B2
# TOOLCHAIN_HINTS must only attach when the server's own message actually
# looks like a missing-toolchain failure.

def test_pdf_500_with_pandoc_missing_gets_the_hint(monkeypatch):
    client, _ = build_client()
    detail = "Pandoc wasn't found. Please check that pandoc is installed."
    fake = _FakeRaw(FakeResponse(500, body=_html_error(detail),
                                 url="http://jupyter.test/nbconvert/pdf/a.ipynb"))
    monkeypatch.setattr(client, "_request_raw", fake)

    with pytest.raises(ExportError) as excinfo:
        export_notebook(client, "pdf", server_path="a.ipynb")

    err = excinfo.value
    assert detail in err.message
    assert err.hint == TOOLCHAIN_HINTS["pdf"]


def test_webpdf_500_with_playwright_missing_gets_the_hint(monkeypatch):
    client, _ = build_client()
    detail = "Playwright is not installed to support Web PDF conversion."
    fake = _FakeRaw(FakeResponse(500, body=_html_error(detail),
                                 url="http://jupyter.test/nbconvert/webpdf/a.ipynb"))
    monkeypatch.setattr(client, "_request_raw", fake)

    with pytest.raises(ExportError) as excinfo:
        export_notebook(client, "webpdf", server_path="a.ipynb")

    assert excinfo.value.hint == TOOLCHAIN_HINTS["webpdf"]


def test_pdf_500_unrelated_failure_carries_no_hint(monkeypatch):
    client, _ = build_client()
    detail = "KeyError: 'nonexistent_variable'"
    fake = _FakeRaw(FakeResponse(500, body=_html_error(detail),
                                 url="http://jupyter.test/nbconvert/pdf/a.ipynb"))
    monkeypatch.setattr(client, "_request_raw", fake)

    with pytest.raises(ExportError) as excinfo:
        export_notebook(client, "pdf", server_path="a.ipynb")

    err = excinfo.value
    assert detail in err.message
    assert err.hint is None


def test_toolchain_hint_helper_is_case_insensitive_and_format_scoped():
    assert _toolchain_hint("pdf", "Pandoc wasn't found") == TOOLCHAIN_HINTS["pdf"]
    assert _toolchain_hint("pdf", "PANDOC WASN'T FOUND") == TOOLCHAIN_HINTS["pdf"]
    # A pandoc-shaped message for a format with no such dependency: no hint.
    assert _toolchain_hint("qtpdf", "Pandoc wasn't found") is None
    assert _toolchain_hint("pdf", None) is None


# --------------------------------------------------------------------- B3
# cells= must be able to carry notebook metadata through to the built
# notebook. (The rest of the pipeline — nbconvert actually reading
# language_info off that metadata to label a Markdown fence — runs on the
# Jupyter server, not in this library, so the boundary this fix owns is
# verified here: does the metadata reach the request.)

def test_cells_export_metadata_reaches_the_built_notebook(monkeypatch):
    client, _ = build_client()
    fake = _FakeRaw(FakeResponse(200, body="```python\nprint(1)\n```",
                                 headers={"Content-Type": "text/markdown"},
                                 url="http://jupyter.test/nbconvert/markdown"))
    monkeypatch.setattr(client, "_request_raw", fake)
    metadata = {"kernelspec": {"name": "python3", "language": "python",
                              "display_name": "Python 3"},
               "language_info": {"name": "python"}}

    export_notebook(client, "markdown",
                    cells=[{"cell_type": "code", "source": "print(1)"}],
                    metadata=metadata)

    sent = fake.calls[0]["json_body"]["content"]
    assert sent["metadata"]["language_info"]["name"] == "python"
    assert sent["metadata"]["kernelspec"]["name"] == "python3"


def test_cells_export_without_metadata_stays_empty(monkeypatch):
    client, _ = build_client()
    fake = _FakeRaw(FakeResponse(200, body="ok",
                                 url="http://jupyter.test/nbconvert/markdown"))
    monkeypatch.setattr(client, "_request_raw", fake)

    export_notebook(client, "markdown",
                    cells=[{"cell_type": "code", "source": "print(1)"}])

    sent = fake.calls[0]["json_body"]["content"]
    assert sent["metadata"] == {}


def test_metadata_rejected_without_cells():
    client, _ = build_client()
    with pytest.raises(JupyterError):
        export_notebook(client, "markdown", server_path="a.ipynb",
                        metadata={"language_info": {"name": "python"}})


# --------------------------------------------------------------------- B4
# Two exports producing the same sidecar name into one directory must raise
# rather than clobber, unless overwrite=True — and nothing gets written once
# any conflict is found, document included.

def test_export_to_path_refuses_to_overwrite_existing_document(tmp_path, monkeypatch):
    client, _ = build_client()
    dest = tmp_path / "out.html"
    dest.write_text("existing content")
    fake = _FakeRaw(FakeResponse(200, body="<html>new</html>",
                                 headers={"Content-Type": "text/html"},
                                 url="http://jupyter.test/nbconvert/html/a.ipynb"))
    monkeypatch.setattr(client, "_request_raw", fake)

    with pytest.raises(ExportError) as excinfo:
        export_notebook(client, "html", server_path="a.ipynb", to_path=str(dest))

    assert excinfo.value.reason == "exists"
    assert str(dest) in excinfo.value.paths
    assert dest.read_text() == "existing content"


def test_export_to_path_overwrite_true_replaces_document(tmp_path, monkeypatch):
    client, _ = build_client()
    dest = tmp_path / "out.html"
    dest.write_text("existing content")
    fake = _FakeRaw(FakeResponse(200, body="<html>new</html>",
                                 headers={"Content-Type": "text/html"},
                                 url="http://jupyter.test/nbconvert/html/a.ipynb"))
    monkeypatch.setattr(client, "_request_raw", fake)

    export_notebook(client, "html", server_path="a.ipynb", to_path=str(dest),
                    overwrite=True)

    assert dest.read_text() == "<html>new</html>"


def test_export_to_path_refuses_to_overwrite_a_sidecar(tmp_path, monkeypatch):
    client, _ = build_client()

    zip1 = _zip_bytes({"doc1.md": b"# doc1\n![img](output_1_0.png)",
                       "output_1_0.png": b"PNGDATA1"})
    fake1 = _FakeRaw(FakeResponse(200, body=zip1,
                                  headers={"Content-Type": "application/zip"},
                                  url="http://jupyter.test/nbconvert/markdown/a.ipynb"))
    monkeypatch.setattr(client, "_request_raw", fake1)
    export_notebook(client, "markdown", server_path="a.ipynb",
                    to_path=str(tmp_path / "doc1.md"))
    assert (tmp_path / "output_1_0.png").read_bytes() == b"PNGDATA1"

    zip2 = _zip_bytes({"doc2.md": b"# doc2\n![img](output_1_0.png)",
                       "output_1_0.png": b"PNGDATA2"})
    fake2 = _FakeRaw(FakeResponse(200, body=zip2,
                                  headers={"Content-Type": "application/zip"},
                                  url="http://jupyter.test/nbconvert/markdown/b.ipynb"))
    monkeypatch.setattr(client, "_request_raw", fake2)

    with pytest.raises(ExportError) as excinfo:
        export_notebook(client, "markdown", server_path="b.ipynb",
                        to_path=str(tmp_path / "doc2.md"))

    err = excinfo.value
    assert err.reason == "exists"
    assert str(tmp_path / "output_1_0.png") in err.paths
    # Nothing was clobbered, and the conflicting export's own document was
    # never written either — the check runs before any write.
    assert (tmp_path / "output_1_0.png").read_bytes() == b"PNGDATA1"
    assert not (tmp_path / "doc2.md").exists()


def test_export_to_path_overwrite_true_replaces_the_sidecar(tmp_path, monkeypatch):
    client, _ = build_client()

    zip1 = _zip_bytes({"doc1.md": b"# doc1\n![img](output_1_0.png)",
                       "output_1_0.png": b"PNGDATA1"})
    fake1 = _FakeRaw(FakeResponse(200, body=zip1,
                                  headers={"Content-Type": "application/zip"},
                                  url="http://jupyter.test/nbconvert/markdown/a.ipynb"))
    monkeypatch.setattr(client, "_request_raw", fake1)
    export_notebook(client, "markdown", server_path="a.ipynb",
                    to_path=str(tmp_path / "doc1.md"))

    zip2 = _zip_bytes({"doc2.md": b"# doc2\n![img](output_1_0.png)",
                       "output_1_0.png": b"PNGDATA2"})
    fake2 = _FakeRaw(FakeResponse(200, body=zip2,
                                  headers={"Content-Type": "application/zip"},
                                  url="http://jupyter.test/nbconvert/markdown/b.ipynb"))
    monkeypatch.setattr(client, "_request_raw", fake2)

    export_notebook(client, "markdown", server_path="b.ipynb",
                    to_path=str(tmp_path / "doc2.md"), overwrite=True)

    assert (tmp_path / "doc2.md").exists()
    assert (tmp_path / "output_1_0.png").read_bytes() == b"PNGDATA2"


# --------------------------------------------------------------------- B5
# A zip served under a differently-normalized content type is still unpacked.

def test_zip_alias_content_type_is_unpacked(monkeypatch):
    client, _ = build_client()
    payload = _zip_bytes({"doc.md": b"# hi", "img.png": b"PNGBYTES"})
    fake = _FakeRaw(FakeResponse(
        200, body=payload, headers={"Content-Type": "application/x-zip-compressed"},
        url="http://jupyter.test/nbconvert/markdown/a.ipynb"))
    monkeypatch.setattr(client, "_request_raw", fake)

    result = export_notebook(client, "markdown", server_path="a.ipynb")

    assert result["bundle"] is True
    assert any(r["name"] == "img.png" for r in result["resources"])


def test_zip_served_as_octet_stream_is_sniffed_by_magic(monkeypatch):
    client, _ = build_client()
    payload = _zip_bytes({"doc.md": b"# hi", "img.png": b"PNGBYTES"})
    fake = _FakeRaw(FakeResponse(
        200, body=payload, headers={"Content-Type": "application/octet-stream"},
        url="http://jupyter.test/nbconvert/markdown/a.ipynb"))
    monkeypatch.setattr(client, "_request_raw", fake)

    result = export_notebook(client, "markdown", server_path="a.ipynb")

    assert result["bundle"] is True
    assert any(r["name"] == "img.png" for r in result["resources"])


def test_non_zip_binary_is_not_mistaken_for_a_bundle(monkeypatch):
    client, _ = build_client()
    payload = b"%PDF-1.4 not actually a zip"
    fake = _FakeRaw(FakeResponse(200, body=payload,
                                 headers={"Content-Type": "application/pdf"},
                                 url="http://jupyter.test/nbconvert/pdf/a.ipynb"))
    monkeypatch.setattr(client, "_request_raw", fake)

    result = export_notebook(client, "pdf", server_path="a.ipynb")

    assert result["bundle"] is False
