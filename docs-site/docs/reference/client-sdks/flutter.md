---
id: flutter_socketio_integration
title: Flutter chat client
---

# Flutter chat client

The official Flutter chat client lives at
[`digitorn_client`](https://github.com/) and is what users
download from the App Store / Play Store / desktop installers.

The Flutter client connects to the daemon over Socket.IO and
renders the same surface as the web client (chat,
workspace, sub-agent activity panel, widgets) using native
mobile and desktop UI controls.

## Build your own Flutter client

The full implementation contract (route surface, dart-side
event types, state shape, theme tokens) is internal team
documentation and is not published.

For an alternative Flutter implementation:

1. The Socket.IO event protocol is documented in
   [Socket.IO Protocol](../api/socketio.md).
2. The YAML language ([language reference](../../language/))
   tells you what shapes to render.

For direct integration, contact your daemon administrator.
