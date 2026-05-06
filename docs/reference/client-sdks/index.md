---
id: client-sdks-index
title: Client SDKs
---

# Client SDKs

| Client | Page | Use it for |
|--------|------|-----------|
| Flutter chat client | [flutter.md](flutter.md) | Mobile and desktop chat UI. Connects via Socket.IO + REST. Lives at `digitorn_client` in the source repo. |
| React preview SDK | [preview-sdk.md](../../language/47-preview-sdk.md) | The `@digitorn/preview-sdk` npm package. Hooks and components used inside `web/` of any preview app to subscribe to workspace state. |
| Python testing SDK | [python-testing.md](python-testing.md) | The `digitorn.testing` package. `DevClient`, `LiveEventStream`, scenario assertions. Used by the test scenarios under `tools/live_tests/` and by external automated test rigs. |
| Web client | [web.md](web.md) | The `digitorn_web` Next.js client. UI surface specification (chat, workspace, sidebar, slash menu, theme). |

## Choosing an SDK

If you are **building a mobile or cross-platform native app**, use
the Flutter client and embed it as a sub-tree of your wider app.

If you are **shipping a Lovable-style web preview** (an in-app
React sandbox the agent writes to), use the React preview SDK
inside `web/` so the user sees state stream in.

If you are **automating tests** (CI, doc verification, scenario
assertions), use the Python testing SDK and write scenarios under
`tools/live_tests/<feature>_scenarios.py`. The SDK is intentionally
minimal: `DevClient.send_live(...)` returns a `LiveEventStream`
that you iterate as a Socket.IO event stream.

If you are **rendering the daemon's own web UI**, that is the web
client and you typically don't extend it - the customisation surface
is `ui:` in the YAML.
