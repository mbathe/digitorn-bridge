"""Example custom app-level middleware: force French responses.

This middleware demonstrates:
- Injecting rules into the system prompt (before)
- Monitoring/transforming the LLM response (after)

Place this file in a `middleware/` folder next to your app YAML,
or reference it with a relative path.
"""


class FrenchOnlyMiddleware:
    """Force the agent to always respond in French."""

    def __init__(self, strict: bool = False):
        self.strict = strict
        self._turns_processed = 0

    async def before(self, ctx):
        """Inject French instruction into system prompt."""
        self._turns_processed += 1

        rule = (
            "\n\n🇫🇷 RÈGLE ABSOLUE: Tu DOIS répondre UNIQUEMENT en français. "
            "Peu importe la langue de l'utilisateur, ta réponse est en français."
        )
        if rule not in ctx.system_prompt:
            ctx.system_prompt += rule

        # Return None = continue to LLM call
        # Return a string = short-circuit (skip LLM, use this as response)
        return None

    async def after(self, ctx, response, tool_calls):
        """Optionally validate the response is in French."""
        # In strict mode, could add validation here
        return response
