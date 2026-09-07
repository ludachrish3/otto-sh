"""Credentials by inventory key, from a pluggable store (spec 2026-09-06 creds-store).

``[creds]`` selects a registered :class:`~otto.creds.protocol.CredsStore`;
``otto.inventory`` wraps the process inventory in a ``CredsOverlay`` that
merges the store's entries under the record's, by login. This package never
imports ``otto.inventory``.
"""

from .config import CompiledCreds as CompiledCreds
from .config import compile_creds as compile_creds
from .config import compile_creds_table as compile_creds_table
from .config import construct_creds_store as construct_creds_store
from .errors import CredsError as CredsError
from .json_store import JsonCredsStore as JsonCredsStore
from .json_store import parse_creds_document as parse_creds_document
from .protocol import CredsStore as CredsStore
from .registry import get_creds_backend_class as get_creds_backend_class
from .registry import register_creds_backend as register_creds_backend
