---
id: client-sdks-index
title: Client SDKs
---

# Client SDKs

| Client | Page | Use it for |
|--------|------|-----------|
| React preview SDK | [preview-sdk.md](../../language/47-preview-sdk.md) | The `@digitorn/preview-sdk` npm package. Hooks and components used inside `web/` of any preview app to subscribe to workspace state. |
| Python testing SDK | [python-testing.md](python-testing.md) | The `digitorn.testing` package. `DevClient`, `LiveEventStream`, scenario assertions. |
| Web client | [web.md](web.md) | The Digitorn web client. UI surface specification (chat, workspace, sidebar, slash menu, theme). |

## Choosing an SDK

If you are **shipping a Lovable-style web preview** (an in-app
React sandbox the agent writes to), use the React preview SDK
inside `web/` so the user sees state stream in.

If you are **automating tests** (CI, doc verification, scenario
assertions), use the Python testing SDK and write scenarios.
The SDK is intentionally minimal: `DevClient.send_live(...)`
returns a `LiveEventStream` you iterate as a stream of events.

If you are **rendering the daemon's own web UI**, that is the
web client and you typically don't extend it - the customisation
surface is `ui:` in the YAML.
