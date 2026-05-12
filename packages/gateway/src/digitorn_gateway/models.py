"""Model alias catalogue.

The gateway exposes user-facing model names like `digitorn-pro` or
`digitorn-fast`. Internally, each alias maps to a real provider/model
id that LiteLLM (or our custom router) understands.

The catalogue lives in a YAML file so an operator can add a model
without redeploying. Reload at runtime via `load_catalog()`.

Schema (see `config/models.yaml.example`):

    models:
      digitorn-pro:
        provider: anthropic
        model: claude-sonnet-4-20250514
        cost_per_1k_input_tokens: 0.003
        cost_per_1k_output_tokens: 0.015
        max_context_tokens: 200000

      digitorn-fast:
        provider: openai
        model: gpt-4o-mini
        cost_per_1k_input_tokens: 0.00015
        cost_per_1k_output_tokens: 0.0006

      mycorp-finetune:
        # When `provider: custom`, the gateway routes the call to
        # `custom_router.handle()` instead of LiteLLM. Use this for
        # in-house models, on-prem deployments, or any backend
        # LiteLLM doesn't cover.
        provider: custom
        model: mycorp-llama-7b
        endpoint: https://internal.mycorp.local/llm
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ModelEntry:
    alias: str
    provider: str
    model: str
    cost_per_1k_input_tokens: float = 0.0
    cost_per_1k_output_tokens: float = 0.0
    cost_per_1k_cache_read_tokens: float = 0.0
    cost_per_1k_cache_write_tokens: float = 0.0
    max_context_tokens: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_custom(self) -> bool:
        """`provider: custom` routes through `custom_router.handle()`
        instead of LiteLLM. The gateway does NOT try to call LiteLLM
        for these - the dispatcher in `llm_call.dispatch()` branches.
        """
        return self.provider.lower() == "custom"

    def litellm_model_id(self) -> str:
        """Identifier passed to `litellm.acompletion(model=...)`.

        For most providers LiteLLM accepts `<provider>/<model>` or just
        `<model>` if the catalogue prefix is unambiguous. Anthropic +
        OpenAI accept the bare model id; for everything else we use
        the explicit prefix to remove ambiguity.
        """
        if self.provider.lower() in ("anthropic", "openai"):
            return self.model
        return f"{self.provider}/{self.model}"


class ModelCatalogue:
    """Thread-safe in-memory model alias map. Hot-reloadable."""

    def __init__(self, entries: dict[str, ModelEntry] | None = None) -> None:
        self._entries: dict[str, ModelEntry] = dict(entries or {})

    def get(self, alias: str) -> ModelEntry | None:
        return self._entries.get(alias)

    def all(self) -> dict[str, ModelEntry]:
        return dict(self._entries)

    def replace(self, entries: dict[str, ModelEntry]) -> None:
        # Whole-map swap so reads during reload either see the old
        # state or the new one, never a torn intermediate. Python dict
        # assignment is atomic at the GIL level, which is enough here.
        self._entries = dict(entries)


def load_catalog(path: str | Path) -> ModelCatalogue:
    """Read a `models.yaml` file and build a ModelCatalogue.

    Returns an empty catalogue (with a warning) when the file is
    missing - the gateway can still serve requests for models the
    catalogue doesn't list, by passing the alias straight to LiteLLM.
    """
    p = Path(path)
    if not p.exists():
        logger.warning(
            "models_catalog_not_found path=%s, gateway will fall through "
            "to LiteLLM-direct for any model alias",
            p,
        )
        return ModelCatalogue()

    import yaml

    with p.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    out: dict[str, ModelEntry] = {}
    for alias, conf in (raw.get("models") or {}).items():
        if not isinstance(conf, dict):
            logger.warning("models_catalog_bad_entry alias=%s ignored", alias)
            continue
        provider = conf.get("provider")
        model = conf.get("model")
        if not provider or not model:
            logger.warning(
                "models_catalog_incomplete alias=%s missing provider or model",
                alias,
            )
            continue
        # Strip the known fields, keep the rest in `extra` for the
        # custom router or per-provider passthroughs.
        extra = {
            k: v for k, v in conf.items()
            if k not in {
                "provider", "model",
                "cost_per_1k_input_tokens",
                "cost_per_1k_output_tokens",
                "cost_per_1k_cache_read_tokens",
                "cost_per_1k_cache_write_tokens",
                "max_context_tokens",
            }
        }
        out[alias] = ModelEntry(
            alias=alias,
            provider=str(provider),
            model=str(model),
            cost_per_1k_input_tokens=float(conf.get("cost_per_1k_input_tokens") or 0.0),
            cost_per_1k_output_tokens=float(conf.get("cost_per_1k_output_tokens") or 0.0),
            cost_per_1k_cache_read_tokens=float(
                conf.get("cost_per_1k_cache_read_tokens") or 0.0
            ),
            cost_per_1k_cache_write_tokens=float(
                conf.get("cost_per_1k_cache_write_tokens") or 0.0
            ),
            max_context_tokens=conf.get("max_context_tokens"),
            extra=extra,
        )

    logger.info("models_catalog_loaded path=%s entries=%d", p, len(out))
    return ModelCatalogue(out)


_catalog: ModelCatalogue | None = None


def get_catalog() -> ModelCatalogue:
    """Return the process-wide catalogue. Lazy-loaded on first call.
    `main.py` populates it explicitly during startup so first request
    is not penalised.
    """
    global _catalog
    if _catalog is None:
        _catalog = ModelCatalogue()
    return _catalog


def set_catalog(catalog: ModelCatalogue) -> None:
    global _catalog
    _catalog = catalog
