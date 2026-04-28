"""Built-in credential handlers.

Importing this package registers all shipped handlers on the default
registry. The daemon's server module imports this once at startup::

    from digitorn.core.credentials import handlers  # noqa: F401

After that, ``default_registry.get(provider_type)`` returns a working
handler for any of:

- ``api_key``, ``multi_field``, ``oauth2``, ``connection_string``,
  ``mcp_server``, ``custom``

Third-party modules can register their own handlers by calling
``default_registry.register(MyHandler())`` - for instance a future
hub package that wants to ship a ``jwt_signed`` handler.
"""

from __future__ import annotations

from digitorn.core.credentials.handler import default_registry
from digitorn.core.credentials.handlers.api_key import ApiKeyHandler
from digitorn.core.credentials.handlers.connection_string import (
    ConnectionStringHandler,
)
from digitorn.core.credentials.handlers.custom import CustomHandler
from digitorn.core.credentials.handlers.mcp_server import McpServerHandler
from digitorn.core.credentials.handlers.multi_field import MultiFieldHandler
from digitorn.core.credentials.handlers.oauth2 import OAuth2Handler

default_registry.register(ApiKeyHandler())
default_registry.register(MultiFieldHandler())
default_registry.register(OAuth2Handler())
default_registry.register(ConnectionStringHandler())
default_registry.register(McpServerHandler())
default_registry.register(CustomHandler())
