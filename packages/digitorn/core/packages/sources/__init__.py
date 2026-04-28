"""Built-in package sources for v1.

Importing this module registers nothing - sources are instantiated
explicitly by the install flow with the right configuration. This
file is just a convenience namespace for the 4 source types.
"""

from digitorn.core.packages.sources.builtin import BuiltinSource
from digitorn.core.packages.sources.git import GitSource
from digitorn.core.packages.sources.hub import HubSource
from digitorn.core.packages.sources.local import LocalSource

__all__ = [
    "BuiltinSource",
    "GitSource",
    "HubSource",
    "LocalSource",
]
