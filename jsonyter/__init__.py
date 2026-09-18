"""jsonyter: a JSON-first Python interface to a Jupyter server.

Every public method returns plain Python objects (dicts, lists, strings,
numbers, booleans, None) that serialize directly with ``json.dumps``, so the
library can sit behind any editor front end — the original target being an
Emacs REPL driven over a JSON pipe (see ``jsonyter.cli``).
"""

from .client import Client, JupyterError
from .export import ExportError
from .kernel import KernelConnection
from .notebook import NotebookConflict, file_hash, read_notebook, write_notebook
# Re-exporting the ``sync`` function here means ``jsonyter.sync`` (attribute
# access on the package) is this function, not the ``jsonyter.sync``
# submodule — the submodule is still reachable via
# ``sys.modules["jsonyter.sync"]`` or ``from jsonyter.sync import ...``, just
# not via ``import jsonyter.sync`` or ``from . import sync`` from inside the
# package. ``jsonyter.cli`` imports names from it directly for this reason.
from .sync import SyncRefused, sync, sync_apply, sync_plan, sync_status
from .transfer import (DEFAULT_CHUNK_SIZE, MAX_SAFE_CHUNK_SIZE, TransferConflict,
                       download, kernel_contents_dir, upload)

__version__ = "2.1.1"

__all__ = ["Client", "KernelConnection", "JupyterError", "NotebookConflict",
           "ExportError", "TransferConflict", "SyncRefused", "read_notebook",
           "write_notebook", "file_hash", "upload", "download",
           "kernel_contents_dir", "sync", "sync_plan", "sync_apply",
           "sync_status", "DEFAULT_CHUNK_SIZE", "MAX_SAFE_CHUNK_SIZE",
           "__version__"]
