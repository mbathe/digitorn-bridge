---
id: api-integration
title: API Integration (private)
---

# API Integration

The HTTP API surface, the OAuth flows for per-app
integrations, and the credentials manifest endpoints are
**not documented publicly**.

Public clients use the SDKs and CLI:

- **Python testing SDK** - [`digitorn.testing`](../reference/client-sdks/python-testing.md)
- **Flutter chat client** - [client-sdks/flutter](../reference/client-sdks/flutter.md)
- **React Preview SDK** - [preview-sdk](47-preview-sdk.md)
- **CLI** - [cli reference](../reference/cli/)

The live event stream over Socket.IO is the one transport
contract that is documented for direct use:
[Socket.IO Protocol](../reference(daemon API).md).

For direct HTTP integration outside of the SDKs, contact your
daemon administrator.
