"""jsonyter: a JSON-first Python interface to a Jupyter server.

Every public method returns plain Python objects (dicts, lists, strings,
numbers, booleans, None) that serialize directly with ``json.dumps``, so the
library can sit behind any editor front end — the original target being an
Emacs REPL driven over a JSON pipe (see ``jsonyter.cli``).
"""

from .client import Client, JupyterError
from .kernel import KernelConnection

__version__ = "0.1.0"

__all__ = ["Client", "KernelConnection", "JupyterError", "__version__"]
