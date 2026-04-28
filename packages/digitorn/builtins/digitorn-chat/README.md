# Digitorn Chat

Your default AI assistant - ask anything, search the web, remember
conversations, write, analyze, brainstorm.

## Why it ships with Digitorn

Digitorn Chat is the **first thing every user sees** when they
install the daemon. It's the on-ramp into the rest of the
ecosystem - once a user has chatted, they understand what
Digitorn agents can do and they're ready to install more
specialised apps.

## What's inside

- **Anthropic Claude** (Haiku 4.5 by default) via the
  ``claude-code`` OAuth fallback so it works on any dev machine
  with Claude Code installed
- **memory** module with auto-remember on, so the assistant
  builds up context about the user across turns
- **web** module for live information lookups
- **context_builder.ask_user** for structured questions back to
  the user when needed

## Permissions

- ✅ Network access (for web search)
- ❌ Filesystem access
- ❌ Shell execution
- 💚 Risk level: **low**

## How to switch the model

The default brain is hardcoded in ``app.yaml``. To use a
different model:

1. Set up your provider's credentials via the credentials form
   (or as a system_wide credential)
2. Edit ``app.yaml`` to reference the new provider/model
3. Re-deploy via ``digitorn package upgrade digitorn-chat``

## Customisation

This package is one of the four built-in apps shipped with the
Digitorn daemon. To customise it without losing your changes on
the next ``pip install -U digitorn``:

1. Copy the package directory to your own location
2. Edit it freely
3. Install your copy as ``my-chat`` via
   ``digitorn package install /path/to/my-chat``
4. The original ``digitorn-chat`` keeps tracking the daemon's
   built-in version
