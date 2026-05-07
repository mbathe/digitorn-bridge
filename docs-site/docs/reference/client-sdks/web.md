---
id: web
title: Web client
---

# Web client

The official web client lives at
[`digitorn_web`](https://github.com/) (Next.js + React 19 +
TypeScript). It is the reference UI consumers see at
`https://app.digitorn.ai`.

The web client connects to the daemon and:

- Lists deployed apps and their sessions.
- Streams turns over Socket.IO with full event handling
  (tokens, tool calls, hooks, workspace updates, sub-agent
  fan-out).
- Renders the workspace pane (live virtual filesystem,
  validation workflow with approve / reject hunks, git
  status).
- Renders declarative widgets defined under `ui.widgets:` in
  YAML.
- Hosts the Lovable-style preview iframe.

## Build your own web client

The full implementation contract (route surface, component
breakdown, state shape, event handling) is internal team
documentation and is not published.

If you are building an alternative web client:

1. Use the [Python testing SDK](python-testing.md) as a
   reference for the daemon's request / response shapes.
2. Use the [Socket.IO Protocol](../api/socketio.md) page for
   the live event stream.
3. Consume the YAML language ([language reference](../../language/))
   so your client renders apps consistently with the official
   web client.

For direct integration, contact your daemon administrator.
