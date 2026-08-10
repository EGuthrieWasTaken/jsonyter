"""Local ``.ipynb`` read/write, independent of any Jupyter server.

These are plain filesystem operations: no server, no kernel, no network. They
exist because serialization has to happen on the Python side — ``nbformat``
round-trips a notebook byte-identically, while a naive JSON re-encode
collapses Jupyter's indentation and turns every save into a whole-file diff.

``write_notebook`` is a read-modify-write against the file on disk: the client
sends cell *source* only, and everything else the file already holds
(``outputs``, ``execution_count``, ``metadata``, ``attachments``, notebook
metadata) is carried across untouched. Execution results are deliberately
never written — see the module's ``write_notebook`` docstring.
"""

import hashlib
import os
import shutil
import tempfile
import uuid

from .client import JupyterError

CELL_TYPES = ("code", "markdown", "raw")

# Cell ids entered nbformat in 4.5; older notebooks must not carry them.
_MINOR_WITH_IDS = 5


class NotebookConflict(JupyterError):
    """The file on disk changed since the client last read it.

    Distinguishable from a generic failure by ``error`` in the JSON payload,
    so a front end can offer a reload instead of clobbering the change.
    """

    def __init__(self, message, path=None, expected=None, actual=None):
        super().__init__(message)
        self.path = path
        self.expected = expected
        self.actual = actual

    def to_json(self):
        payload = super().to_json()
        payload.update({"path": self.path, "expected_hash": self.expected,
                        "actual_hash": self.actual})
        return payload


def _nbformat():
    """Import nbformat lazily, with an actionable error if it's missing."""
    try:
        import nbformat
        return nbformat
    except ImportError as exc:      # pragma: no cover - depends on install
        raise JupyterError(
            "the notebook methods require nbformat: pip install nbformat"
        ) from exc


# --------------------------------------------------------------------- paths

def _resolve(path):
    if not path or not isinstance(path, str):
        raise JupyterError("missing or invalid param: path")
    return os.path.abspath(os.path.expanduser(path))


def file_hash(path):
    """sha256 of the file's bytes, or ``None`` if it doesn't exist."""
    path = _resolve(path)
    if not os.path.exists(path):
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ----------------------------------------------------------------- cell ids

def _new_cell_id():
    try:
        from nbformat.v4.nbbase import random_cell_id
        return random_cell_id()
    except Exception:               # pragma: no cover - older nbformat
        return uuid.uuid4().hex[:8]


def _supports_ids(nb):
    return (nb.get("nbformat", 4) > 4
            or nb.get("nbformat_minor", 0) >= _MINOR_WITH_IDS)


def _ensure_ids(nb):
    """Give every cell a unique id, upgrading nbformat_minor if needed."""
    if nb.get("nbformat", 4) == 4 and not _supports_ids(nb):
        nb["nbformat_minor"] = _MINOR_WITH_IDS
    seen = set()
    for cell in nb.cells:
        cell_id = cell.get("id")
        if not cell_id or cell_id in seen:
            cell_id = _new_cell_id()
            cell["id"] = cell_id
        seen.add(cell_id)
    return nb


def _strip_ids(nb):
    """Remove cell ids: they are invalid before nbformat 4.5."""
    for cell in nb.cells:
        cell.pop("id", None)
    return nb


# -------------------------------------------------------------------- read

def _read_v4(path):
    """Read a notebook of any version as nbformat v4, without touching ids."""
    nbformat = _nbformat()
    try:
        return nbformat.read(path, as_version=4)
    except FileNotFoundError as exc:
        raise JupyterError("no such notebook: {}".format(path)) from exc
    except JupyterError:
        raise
    except Exception as exc:
        raise JupyterError(
            "could not read notebook {}: {}".format(path, exc)) from exc


def read_notebook(path):
    """Read ``path`` as normalized nbformat v4 with an id on every cell.

    Older notebooks (nbformat 3, or 4.0–4.4 without ids) are upgraded in
    memory so the id-based merge in :func:`write_notebook` has something to
    match on. The file itself is not modified.
    """
    return _ensure_ids(_read_v4(_resolve(path)))


# ------------------------------------------------------------------- write

def _normalize_source(source):
    if source is None:
        return ""
    if isinstance(source, list):
        return "".join(source)
    if not isinstance(source, str):
        raise JupyterError(
            "cell source must be a string, got {}".format(type(source).__name__))
    return source


def _current_source(cell):
    source = cell.get("source", "")
    return "".join(source) if isinstance(source, list) else source


def _validate_specs(cells):
    if not isinstance(cells, list):
        raise JupyterError("missing or invalid param: cells (expected a list)")
    specs = []
    for index, spec in enumerate(cells):
        if not isinstance(spec, dict):
            raise JupyterError(
                "cells[{}] must be an object, got {}".format(
                    index, type(spec).__name__))
        cell_type = spec.get("cell_type", "code")
        if cell_type not in CELL_TYPES:
            raise JupyterError(
                "cells[{}] has invalid cell_type {!r} (expected one of {})"
                .format(index, cell_type, ", ".join(CELL_TYPES)))
        specs.append({
            "id": spec.get("id"),
            "cell_type": cell_type,
            "source": _normalize_source(spec.get("source", "")),
        })
    return specs


def _retype(cell, cell_type):
    """Change a cell's type, dropping fields the new type can't carry."""
    cell["cell_type"] = cell_type
    if cell_type == "code":
        # Only code cells may carry outputs; only non-code may carry
        # attachments. Both directions have to be cleaned or validation fails.
        cell.pop("attachments", None)
        cell["outputs"] = []
        cell["execution_count"] = None
    else:
        cell.pop("outputs", None)
        cell.pop("execution_count", None)


def _new_cell(cell_type, source, with_id):
    nbformat = _nbformat()
    factory = {"code": nbformat.v4.new_code_cell,
               "markdown": nbformat.v4.new_markdown_cell,
               "raw": nbformat.v4.new_raw_cell}[cell_type]
    cell = factory(source=source)
    if with_id:
        if not cell.get("id"):
            cell["id"] = _new_cell_id()
    else:
        cell.pop("id", None)
    return cell


def _merge_cells(nb, specs):
    """Rebuild ``nb.cells`` from ``specs``, preserving matched cells."""
    existing = list(nb.cells)
    with_ids = _supports_ids(nb) and any(cell.get("id") for cell in existing)
    by_id = {}
    if with_ids:
        for cell in existing:
            if cell.get("id"):
                by_id.setdefault(cell["id"], cell)

    consumed = set()
    merged = []
    for index, spec in enumerate(specs):
        match = None
        if with_ids:
            if spec["id"] is not None:
                candidate = by_id.get(spec["id"])
                if candidate is not None and id(candidate) not in consumed:
                    match = candidate
        elif index < len(existing) and id(existing[index]) not in consumed:
            # nbformat < 4.5 has no ids to match on, so position is all we
            # have; this preserves outputs for straight in-place edits.
            match = existing[index]

        if match is None:
            merged.append(_new_cell(spec["cell_type"], spec["source"],
                                    _supports_ids(nb)))
            continue

        consumed.add(id(match))
        if match.get("cell_type") != spec["cell_type"]:
            _retype(match, spec["cell_type"])
        # Only touch source when it actually changed, so an unedited save
        # stays byte-identical to the original file.
        if _current_source(match) != spec["source"]:
            match["source"] = spec["source"]
        merged.append(match)

    nb.cells = merged
    if _supports_ids(nb):
        _ensure_ids(nb)
    else:
        _strip_ids(nb)
    return nb


def _atomic_write(nb, path):
    """Serialize to a temp file in the same directory, then os.replace()."""
    nbformat = _nbformat()
    directory = os.path.dirname(path) or "."
    handle = None
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=directory, prefix=".jsonyter-", suffix=".ipynb")
        handle = os.fdopen(fd, "w", encoding="utf-8")
        nbformat.write(nb, handle, version=nbformat.NO_CONVERT)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        handle = None
        if os.path.exists(path):
            shutil.copymode(path, tmp_path)      # keep the original's mode
        os.replace(tmp_path, path)               # atomic; never truncates
        tmp_path = None
    except JupyterError:
        raise
    except Exception as exc:
        raise JupyterError(
            "could not write notebook {}: {}".format(path, exc)) from exc
    finally:
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass
        if tmp_path is not None and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def write_notebook(path, cells, expect_hash=None):
    """Merge ``cells`` (source only) into the notebook at ``path``.

    ``cells`` is a list of ``{"id": ..., "cell_type": ..., "source": ...}``.
    The notebook on disk is read first and the cell list rebuilt in the given
    order:

    - an ``id`` matching an existing cell reuses that cell, replacing only
      ``source`` — ``outputs``, ``execution_count``, ``metadata`` and
      ``attachments`` survive, so reordering and editing keep their results;
    - ``id: null`` or an unknown id creates a fresh cell;
    - an existing cell not listed is deleted;
    - a changed ``cell_type`` drops that cell's ``outputs`` and
      ``execution_count`` (and ``attachments`` when becoming code).

    Notebook-level ``metadata``, ``nbformat`` and ``nbformat_minor`` are
    preserved. Notebooks older than nbformat 4.5 have no cell ids, so cells
    are matched by position instead and no ids are written back.

    Outputs are never written: the client doesn't send them and execution
    results are session-only. Stored outputs are preserved, never updated.

    ``expect_hash`` is the sha256 the client last saw; if the file no longer
    matches, :class:`NotebookConflict` is raised and nothing is written. The
    write itself goes to a temp file in the same directory and is moved into
    place with ``os.replace``, so an interrupted save can never truncate the
    original. The notebook is validated before any of that happens.

    Returns ``{"path", "cells": [ids in order], "written": True, "hash"}``.
    """
    nbformat = _nbformat()
    path = _resolve(path)
    specs = _validate_specs(cells)

    if expect_hash is not None:
        actual = file_hash(path)
        if actual != expect_hash:
            raise NotebookConflict(
                "notebook on disk has changed since it was read; refusing to "
                "overwrite {}".format(path),
                path=path, expected=expect_hash, actual=actual)

    if os.path.exists(path):
        # Read as v4 but leave ids alone: adding them here would rewrite a
        # pre-4.5 notebook's format as a side effect of an ordinary save.
        nb = _read_v4(path)
    else:
        # Saving a notebook that doesn't exist yet creates it.
        nb = nbformat.v4.new_notebook()

    _merge_cells(nb, specs)

    try:
        nbformat.validate(nb)
    except Exception as exc:
        raise JupyterError(
            "refusing to write invalid notebook {}: {}".format(path, exc)
        ) from exc

    _atomic_write(nb, path)
    return {"path": path,
            "cells": [cell.get("id") for cell in nb.cells],
            "written": True,
            "hash": file_hash(path)}
