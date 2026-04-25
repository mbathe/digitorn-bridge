"""Model price book — USD per 1M tokens, prompt & completion.

Hard-coded defaults covering the models Digitorn ships support
for. Admins can override via ``DIGITORN_MODEL_PRICES_PATH``
pointing at a JSON file::

    {
      "claude-opus-4-6":     { "prompt": 15.0, "completion": 75.0 },
      "my-custom-model":     { "prompt": 1.0,  "completion": 3.0  }
    }

Values are in USD per 1M tokens (standard billing unit for LLM
providers). When a model is unknown, we fall back to a generic
$3 / $15 estimate — better than showing $0 which would hide
cost from the user.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelPrice:
    """USD per 1M tokens."""
    prompt: float
    completion: float


# Defaults — kept intentionally short, updated as pricing changes.
# The key is the **short model id** as reported by the LLM provider;
# we match by substring (case-insensitive) so "claude-opus-4-6-20250101"
# matches "claude-opus-4-6".
_DEFAULT_PRICES: dict[str, ModelPrice] = {
    # Anthropic
    "claude-opus-4-6":      ModelPrice(prompt=15.0, completion=75.0),
    "claude-sonnet-4-6":    ModelPrice(prompt=3.0,  completion=15.0),
    "claude-sonnet-4-5":    ModelPrice(prompt=3.0,  completion=15.0),
    "claude-haiku-4-5":     ModelPrice(prompt=0.80, completion=4.0),
    "claude-haiku":         ModelPrice(prompt=0.80, completion=4.0),
    "claude-sonnet":        ModelPrice(prompt=3.0,  completion=15.0),
    "claude-opus":          ModelPrice(prompt=15.0, completion=75.0),
    # OpenAI
    "gpt-4o-mini":          ModelPrice(prompt=0.15, completion=0.60),
    "gpt-4o":               ModelPrice(prompt=2.50, completion=10.0),
    "gpt-4-turbo":          ModelPrice(prompt=10.0, completion=30.0),
    "gpt-4":                ModelPrice(prompt=30.0, completion=60.0),
    "gpt-3.5":              ModelPrice(prompt=0.50, completion=1.50),
    "o1":                   ModelPrice(prompt=15.0, completion=60.0),
    "o1-mini":              ModelPrice(prompt=3.0,  completion=12.0),
    # DeepSeek
    "deepseek-chat":        ModelPrice(prompt=0.27, completion=1.10),
    "deepseek-reasoner":    ModelPrice(prompt=0.55, completion=2.20),
    # Google
    "gemini-1.5-pro":       ModelPrice(prompt=1.25, completion=5.0),
    "gemini-1.5-flash":     ModelPrice(prompt=0.075, completion=0.30),
    "gemini-2.0":           ModelPrice(prompt=1.25, completion=5.0),
    # Mistral
    "mistral-large":        ModelPrice(prompt=2.0,  completion=6.0),
    "mistral-small":        ModelPrice(prompt=0.20, completion=0.60),
    # Groq (hosted OSS, near-free)
    "llama-3":              ModelPrice(prompt=0.05, completion=0.08),
    "llama-3.1":            ModelPrice(prompt=0.05, completion=0.08),
    "mixtral":              ModelPrice(prompt=0.24, completion=0.24),
}

# Fallback when the model name matches nothing — avoids showing $0
# for unknown providers while still flagging "approximate" to the UI.
_UNKNOWN_PRICE = ModelPrice(prompt=3.0, completion=15.0)


class ModelPriceBook:
    """Resolve a USD price for a model name.

    The book is a singleton loaded at daemon startup. Admins point
    ``DIGITORN_MODEL_PRICES_PATH`` at a JSON file to override or
    extend the defaults without touching code.
    """

    def __init__(
        self,
        overrides: dict[str, ModelPrice] | None = None,
    ) -> None:
        self._prices: dict[str, ModelPrice] = dict(_DEFAULT_PRICES)
        if overrides:
            self._prices.update(overrides)

    @classmethod
    def from_env(cls) -> "ModelPriceBook":
        path = os.environ.get("DIGITORN_MODEL_PRICES_PATH", "").strip()
        if not path or not os.path.exists(path):
            return cls()
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            overrides = {
                str(k): ModelPrice(
                    prompt=float(v.get("prompt", 0.0)),
                    completion=float(v.get("completion", 0.0)),
                )
                for k, v in (data or {}).items()
            }
            logger.info(
                "ModelPriceBook: loaded %d overrides from %s",
                len(overrides), path,
            )
            return cls(overrides=overrides)
        except Exception as exc:
            logger.warning("ModelPriceBook: failed to load %s: %s", path, exc)
            return cls()

    def price_for(self, model: str) -> ModelPrice:
        """Resolve a model name to its price. Matches by substring
        against the configured keys (longest match wins) so versioned
        ids like ``claude-opus-4-6-20250101`` still find the base
        entry."""
        if not model:
            return _UNKNOWN_PRICE
        m = model.lower()
        # Exact match first
        if m in self._prices:
            return self._prices[m]
        # Longest substring match — critical for versioned model ids
        best: tuple[str, ModelPrice] | None = None
        for key, price in self._prices.items():
            if key in m:
                if best is None or len(key) > len(best[0]):
                    best = (key, price)
        return best[1] if best else _UNKNOWN_PRICE

    def compute_cost(
        self, model: str, prompt_tokens: int, completion_tokens: int,
    ) -> float:
        """USD cost for a single LLM call."""
        p = self.price_for(model)
        return round(
            (prompt_tokens * p.prompt + completion_tokens * p.completion)
            / 1_000_000.0,
            6,
        )


_default_book: ModelPriceBook | None = None


def compute_cost(
    model: str, prompt_tokens: int, completion_tokens: int,
) -> float:
    """Convenience wrapper around the default (singleton) price book."""
    global _default_book
    if _default_book is None:
        _default_book = ModelPriceBook.from_env()
    return _default_book.compute_cost(model, prompt_tokens, completion_tokens)
