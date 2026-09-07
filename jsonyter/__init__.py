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
from .transfer import (DEFAULT_CHUNK_SIZE, MAX_SAFE_CHUNK_SIZE, TransferConflict,
                       download, kernel_contents_dir, upload)

__version__ = "1.2.0"

__all__ = ["Client", "KernelConnection", "JupyterError", "NotebookConflict",
           "ExportError", "TransferConflict", "read_notebook", "write_notebook",
           "file_hash", "upload", "download", "kernel_contents_dir",
           "DEFAULT_CHUNK_SIZE", "MAX_SAFE_CHUNK_SIZE", "__version__"]
