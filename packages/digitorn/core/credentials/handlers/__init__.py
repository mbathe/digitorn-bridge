"""Built-in credential handlers."""

from __future__ import annotations

from digitorn.core.credentials.handler import default_registry
from digitorn.core.credentials.handlers.api_key import ApiKeyHandler
from digitorn.core.credentials.handlers.aws_access_key import AwsAccessKeyHandler
from digitorn.core.credentials.handlers.azure_ad import AzureAdHandler
from digitorn.core.credentials.handlers.basic_auth import BasicAuthHandler
from digitorn.core.credentials.handlers.bearer_token import BearerTokenHandler
from digitorn.core.credentials.handlers.client_certificate import (
    ClientCertificateHandler,
)
from digitorn.core.credentials.handlers.connection_string import (
    ConnectionStringHandler,
)
from digitorn.core.credentials.handlers.custom import CustomHandler
from digitorn.core.credentials.handlers.database_fields import (
    DatabaseFieldsHandler,
)
from digitorn.core.credentials.handlers.device_code import DeviceCodeHandler
from digitorn.core.credentials.handlers.file_upload import FileUploadHandler
from digitorn.core.credentials.handlers.gcp_service_account import (
    GcpServiceAccountHandler,
)
from digitorn.core.credentials.handlers.hmac_signing_secret import (
    HmacSigningSecretHandler,
)
from digitorn.core.credentials.handlers.mcp_http import McpHttpHandler
from digitorn.core.credentials.handlers.mcp_server import McpServerHandler
from digitorn.core.credentials.handlers.multi_field import MultiFieldHandler
from digitorn.core.credentials.handlers.oauth2 import OAuth2Handler
from digitorn.core.credentials.handlers.oauth2_pkce import OAuth2PkceHandler
from digitorn.core.credentials.handlers.ssh_key import SshKeyHandler

# Token / api-key family
default_registry.register(ApiKeyHandler())
default_registry.register(BearerTokenHandler())
default_registry.register(BasicAuthHandler())
# OAuth flavours
default_registry.register(OAuth2Handler())
default_registry.register(OAuth2PkceHandler())
default_registry.register(DeviceCodeHandler())
# Multi-field structured
default_registry.register(MultiFieldHandler())
default_registry.register(ConnectionStringHandler())
default_registry.register(DatabaseFieldsHandler())
# Cloud providers
default_registry.register(AwsAccessKeyHandler())
default_registry.register(GcpServiceAccountHandler())
default_registry.register(AzureAdHandler())
# Files / certs
default_registry.register(SshKeyHandler())
default_registry.register(ClientCertificateHandler())
default_registry.register(FileUploadHandler())
# Webhook signing
default_registry.register(HmacSigningSecretHandler())
# MCP
default_registry.register(McpServerHandler())
default_registry.register(McpHttpHandler())
# Catch-all
default_registry.register(CustomHandler())
