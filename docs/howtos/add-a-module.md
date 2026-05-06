---
id: add-a-module
title: How to add a module
---

# How to add a module

A module is a Python package under `packages/digitorn/modules/<id>/`
that subclasses `BaseModule` and decorates agent-callable methods
with `@action`. Adding a module makes its tools available to every
app that lists it under `tools.modules.<id>` in YAML.

This page walks through the mechanics with a minimal example.
For the full reference of `@action` flags, slots, capabilities,
and constraint specs, see the [Module reference index](../reference/modules/).

## Layout

```
packages/digitorn/modules/my_module/
├── __init__.py
├── digitorn-module.toml         # catalogue metadata
├── module.py                    # BaseModule subclass
├── params.py                    # Pydantic params + return models
└── README.md                    # optional - dev notes
```

## 1. The module class

`module.py`:

```python
from __future__ import annotations
from digitorn.modules.base import ActionResult, BaseModule
from digitorn.modules.decorators import action
from digitorn.modules.my_module.params import GreetParams, GreetResult


class MyModule(BaseModule):
    """A trivial example module with one agent-callable action."""

    @action(
        description="Say hello to a name.",
        params_model=GreetParams,
        cli_label="Greet",
        risk_level="low",
        tool_prompt=(
            "Greet a person by name. Use this when the user asks for "
            "a personalised greeting; otherwise reply directly without "
            "calling this tool."
        ),
    )
    async def greet(self, params: GreetParams) -> ActionResult:
        text = f"Hello, {params.name}!"
        return ActionResult(success=True, data=GreetResult(message=text))
```

Things to know:

- The class **must** subclass `BaseModule`.
- Action methods are decorated with `@action(...)` and **must** be
  `async`.
- Method signature is `async def <name>(self, params: <Model>) -> ActionResult`.
- `params_model` is the Pydantic `BaseModel` the framework
  validates the LLM's call against.
- `description` is the short (1 line) text the LLM sees in the
  tool index. Keep it under ~80 characters.
- `tool_prompt` is detailed instructions injected into the system
  prompt. Use it when the agent needs to know **when** to call the
  tool vs alternatives. Empty for self-explanatory tools.
- `risk_level` is `low | medium | high`. The capabilities gate
  consults this when applying `default_policy: auto`.

## 2. The params and result

`params.py`:

```python
from pydantic import BaseModel, Field


class GreetParams(BaseModel):
    """Parameters for `my_module.greet`."""
    name: str = Field(..., min_length=1, max_length=200,
                      description="Person to greet.")


class GreetResult(BaseModel):
    """Return shape of `my_module.greet`."""
    message: str = Field(..., description="Composed greeting text.")
```

The params model is the source of truth for the JSON schema the
LLM receives. `Field(..., description=...)` text shows up in the
tool's parameter docs. Hidden params (advanced flags users
shouldn't see) take `json_schema_extra={"hidden": True}`.

## 3. The catalogue manifest

`digitorn-module.toml`:

```toml
[module]
id = "my_module"
name = "My Module"
version = "1.0.0"
description = "A trivial example module."
category = "general"

[module.requires]
# OS / runtime / Python deps the daemon should install.
# Example:
# pip = ["requests>=2.31"]
# system = ["curl"]
```

The daemon reads this at startup to populate the
`/api/discovery/modules` endpoint and the `digitorn modules list`
CLI command.

## 4. Wire it in

Modules are auto-discovered from `packages/digitorn/modules/`. No
explicit registration needed. Restart the daemon (`digitorn stop`
+ `digitorn start`) and your module is loaded.

Verify with the CLI:

```bash
digitorn modules list | grep my_module
digitorn app schema my_module
```

## 5. Use it from a YAML app

```yaml
app:
  app_id: hello-app
  name: "Hello App"

agents:
  - id: assistant
    role: assistant
    brain:
      provider: ollama
      model: qwen25-7b-gpu:latest
      backend: openai_compat
      config:
        base_url: http://localhost:11434/v1
        api_key: ollama
    system_prompt: "Use the Greet tool when the user asks to be greeted."

tools:
  modules:
    my_module: {}
  capabilities:
    default_policy: auto
```

Deploy + chat:

```bash
digitorn dev deploy hello-app.yaml
digitorn dev chat hello-app -m "Greet me, I'm Paul."
```

The agent should call `my_module.greet(name="Paul")` and respond
with the result.

## What to read next

- [Module reference index](../reference/modules/) - every shipped
  module, formatted the way yours will appear.
- [`packages/digitorn/modules/filesystem/`](https://github.com/) -
  a real-world example with five actions, fuzzy edit matching,
  and a constraint spec.
- [Tools reference](../language/04-tools.md) - the YAML surface
  apps use to wire your module in.
