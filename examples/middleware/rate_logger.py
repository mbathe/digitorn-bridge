"""Example custom module-level middleware: log every tool call with timing.

Module-level middlewares wrap individual module actions (filesystem.read,
database.query, etc.) and follow the __call__(ctx, next_) protocol.
"""

import time


class RateLoggerMiddleware:
    """Log every module call with timing and rate info."""

    def __init__(self, warn_after_ms: float = 1000.0):
        self.warn_after_ms = warn_after_ms
        self._call_count = 0
        self._slow_calls = 0

    async def __call__(self, ctx, next_):
        """Wrap the module action execution.

        Args:
            ctx: ModuleCallContext with module_id, action, params, metadata
            next_: async callable to invoke the next middleware (or the handler)
        """
        self._call_count += 1
        start = time.monotonic()

        # Call the actual module action (or next middleware)
        result = await next_(ctx)

        duration_ms = (time.monotonic() - start) * 1000
        if duration_ms > self.warn_after_ms:
            self._slow_calls += 1
            print(
                f"⚠️ SLOW CALL [{duration_ms:.0f}ms] "
                f"{ctx.module_id}.{ctx.action}"
            )
        else:
            print(
                f"✅ [{duration_ms:.0f}ms] "
                f"{ctx.module_id}.{ctx.action}"
            )

        return result
