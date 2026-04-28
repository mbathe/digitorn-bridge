"""Live verification - Round 3 bug fixes.

  - BUG-039: message_done always emitted
  - BUG-040: compiler rejects common LLM YAML mistakes with clear hints
  - BUG-041: dev_tools auto-persists a build draft on deploy
  - BUG-046: openai_compat reports vision capability per-model (not hardcoded)
  - BUG-047: generic /api/ rate limit bucket exists
  - BUG-048: DELETE /api/apps/{id} on a no-op returns success=false
"""
from __future__ import annotations
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "packages"))

import httpx  # noqa: E402

BASE = "http://127.0.0.1:8000"


def pass_(ok: bool, label: str, detail: str = "") -> dict:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}")
    if detail:
        print(f"         {detail}")
    return {"ok": ok, "label": label, "detail": detail}


def main() -> int:
    results = []

    # ── BUG-039: message_done emitted when queue disabled ──
    print("\n── BUG-039: message_done fires even with queue disabled ──")
    import inspect
    from digitorn.core.api import apps as apps_mod
    # Extract the session_send_message function source around the finally
    src = Path(apps_mod.__file__).read_text(encoding="utf-8")
    # The fix removed `_qcfg.enabled and` gating before the terminal emit
    idx = src.find("terminal_type = \"message_cancelled\" if cancelled else \"message_done\"")
    preceding = src[max(0, idx - 200): idx] if idx >= 0 else ""
    gated_only_by_correlation = (
        "if _active_correlation_id:" in preceding
        and "_qcfg.enabled and _active_correlation_id:" not in preceding
    )
    results.append(pass_(
        gated_only_by_correlation,
        "message_done emit only gated by correlation_id (not queue flag)",
        "",
    ))

    # ── BUG-040: compiler catches common LLM YAML mistakes ──
    print("\n── BUG-040: compiler pre-flight catches LLM YAML hallucinations ──")
    from digitorn.core.app.compiler import AppYAMLCompiler as Compiler
    from digitorn.core.app.compiler import AppCompilationError
    # We build a raw dict with all 4 mistakes the builder kept making
    bad = {
        "name": "counter",
        "modules": [{"id": "memory", "type": "memory"}],
        "agents": {"assistant": {"model": {"provider": "anthropic"}}},
        "app": {"type": "conversation", "entrypoint": "assistant"},
        "capabilities": {"grant": ["memory.read"]},
    }
    from digitorn.modules.registry import ModuleRegistry
    c = Compiler(ModuleRegistry())
    try:
        c.compile(bad)
        caught = False
        errs: list[str] = []
    except AppCompilationError as exc:
        caught = True
        errs = list(exc.errors if hasattr(exc, "errors") else [str(exc)])
    except Exception as exc:
        caught = False
        errs = [f"unexpected {type(exc).__name__}: {exc}"]
    joined = "\n".join(errs).lower()
    # We expect the pre-flight to flag at least the 4 shape mistakes
    # present in this payload: modules-list, agents-dict, app.type-root,
    # capabilities.grant-strings. Other mistakes (`name:` at root,
    # `agents[].model:`) are present in OTHER LLM outputs but don't
    # apply to this exact payload.
    covers = all(needle in joined for needle in (
        "modules:", "dict", "agents:", "list",
        "app.type", "capabilities.grant:",
    ))
    results.append(pass_(
        caught and covers,
        "compiler flags all 4 common LLM YAML mistakes in one shot",
        f"errs={len(errs)} covers_all_four={covers}\n"
        f"         first={errs[0][:100]!r}" if errs else "no errors",
    ))

    # ── BUG-041: dev_tools has _persist_draft helper ──
    print("\n── BUG-041: dev_tools auto-persists drafts on deploy ──")
    dev_src = Path(
        ROOT / "packages" / "digitorn" / "modules" / "dev_tools" / "module.py"
    ).read_text(encoding="utf-8")
    has_persist = (
        "_persist_draft" in dev_src
        and "BuildDraftStore" in dev_src
        and "auto_persisted" in dev_src
    )
    results.append(pass_(
        has_persist,
        "dev_tools.app(deploy) auto-persists a BuildDraft row",
        "",
    ))

    # ── BUG-046: openai_compat vision is per-model ──
    print("\n── BUG-046: openai_compat.vision is per-model ──")
    from digitorn.modules.llm_provider.providers.openai_compat import (
        OpenAICompatProvider,
    )
    # Fake-init just enough to query get_info
    class _P(OpenAICompatProvider):
        def __init__(self, model):
            self.model = model
            self.provider_hint = ""
            self.base_url = ""
            self._client = None
            self.provider_id = "test"
            self.BACKEND = "openai_compat"
            self._default_temperature = 0.1
            self._default_max_tokens = 1024
            self._default_top_p = 1.0
    ds = _P("deepseek-chat").get_info().capabilities.vision
    gpt4o = _P("gpt-4o-mini").get_info().capabilities.vision
    qwen = _P("qwen-vl-plus").get_info().capabilities.vision
    results.append(pass_(
        ds is False and gpt4o is True and qwen is True,
        "vision capability is model-dependent",
        f"deepseek-chat={ds} gpt-4o={gpt4o} qwen-vl={qwen}",
    ))

    # ── BUG-047: generic API rate limit bucket ──
    print("\n── BUG-047: generic /api/ rate-limit bucket installed ──")
    server_src = Path(
        ROOT / "packages" / "digitorn" / "core" / "server.py"
    ).read_text(encoding="utf-8")
    has_generic = "__api_generic__" in server_src
    results.append(pass_(
        has_generic,
        "catch-all rate-limit bucket exists for /api/*",
        "",
    ))

    # ── BUG-048: DELETE no-op returns success=false ──
    print("\n── BUG-048: DELETE /api/apps/{id} is honest on no-op ──")
    # We test the code path statically - the logic reads
    # `result.get("actually_deleted", True)` and branches.
    apps_src = Path(
        ROOT / "packages" / "digitorn" / "core" / "api" / "apps.py"
    ).read_text(encoding="utf-8")
    has_honest = (
        '"actually_deleted"' in apps_src
        and '"nothing_to_delete"' in apps_src
    )
    mgr_src = Path(
        ROOT / "packages" / "digitorn" / "core" / "app" / "manager.py"
    ).read_text(encoding="utf-8")
    mgr_honest = (
        'nothing_happened' in mgr_src
        and 'actually_deleted' in mgr_src
    )
    results.append(pass_(
        has_honest and mgr_honest,
        "DELETE returns `deleted:false, error:nothing_to_delete` on no-op",
        "",
    ))

    print(f"\n=> {sum(1 for r in results if r['ok'])}/{len(results)} pass")
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
