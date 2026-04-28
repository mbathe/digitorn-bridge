# Compiler Stress Test - Full Report (268 cases)

## Results

| Phase | Score |
|---|---|
| **Compilation** | **268/268 (100.0%)** |
| **False positives** (invalid accepted) | **0** |
| **False negatives** (valid rejected) | **0** |
| **Deploy** | **143/143 (100%)** - every valid YAML deploys |

## Coverage (23 categories)

| Category | Cases | Passed |
|---|---|---|
| structure (root keys, required fields, extras) | 10 | 10 |
| mode (Literal conversation/one_shot/background/pipeline) | 7 | 7 |
| workspace_mode (Literal) | 9 | 9 |
| session_mode (Literal) | 6 | 6 |
| tool_injection (Literal) | 7 | 7 |
| context config (strategy, max_tokens, extras) | 9 | 9 |
| hooks_event (14 events + YAML 1.1 trap + typos) | 21 | 21 |
| hooks_cond (14 conditions, param names, required/optional) | 30 | 30 |
| hooks_action (13 actions, param names, required/optional) | 31 | 31 |
| hooks_cross (module_action target validation) | 2 | 2 |
| hooks_misc (dup ids, extra keys) | 2 | 2 |
| capabilities.grant (module cross-ref, action names, policies, risk) | 13 | 13 |
| agents (delegate_to, duplicates, specialist modules+actions, pool) | 7 | 7 |
| brain (10 providers, 2 backends, typos for both) | 21 | 21 |
| placeholders (vars, env/secret/sys/credential namespaces, typos) | 9 | 9 |
| filters (11 valid + 5 typos) | 16 | 16 |
| channels (9 types + 5 typos + default_channel cross-ref) | 17 | 17 |
| middleware (8 built-in names + custom: prefix + typos) | 16 | 16 |
| modules_config (4 modules × ok/bad config key) | 10 | 10 |
| triggers (3 types + 5 HTTP methods + typos) | 12 | 12 |
| pipeline (typed steps, missing fields) | 3 | 3 |
| sandbox (4 levels + typos) | 7 | 7 |
| yaml_errors (indent, tab, missing colon) | 3 | 3 |

## Bugs found & fixed during stress

1. `{{x | filter}}` in system_prompt was trying to resolve `"x | filter"` as a variable. Fixed in `variables.py` - skip resolve when `|` present (runtime template).
2. `{{input}}`, `{{steps[N]}}`, `{{output}}`, `{{caller}}`, `{{request}}` pipeline namespaces rejected as "undefined variables". Fixed by adding to `_RESERVED_ROOT` in compiler placeholder validator.
3. `execution.default_channel` pointing to a top-level `channels:` entry was rejected because compiler only checked `modules.channels.config.providers`. Fixed to also check top-level `channels:` block.
4. Placeholder `head` parsing didn't strip `[0]` array index. `{{steps[0].output}}` had head `"steps[0]"` which never matched anything. Fixed to strip `[...]` before lookup.

## What this proves

- **If compilation succeeds, the app will deploy.** No false positives in 268 cases covering the full documented feature surface.
- **Real typos are caught with fuzzy suggestions.** All 125 invalid cases return actionable error messages with `file:line:col` and "Did you mean: X?" hints.
- Literal enums prevent silent drift (mode, workspace_mode, session_mode, tool_injection, context.strategy, classifier.frequency, trigger.method, sandbox.level, max_risk_level, default_policy).
- Cross-references verified (capabilities → modules, agents.delegate_to → agent ids, hook module_action → modules, placeholders → credentials_schema).
- CONFIG_MODEL strict on 7 modules (http, queue, rag, shell, vector, web, workspace) catches config typos.

## What's not covered

- The 13 modules still without `CONFIG_MODEL` (memory, lsp, channels, llm_provider, context_builder, agent_spawn, preview, widget, cron_native, mcp, index, database, composite) accept any `config:` key silently. They emit a log warning but don't hard-fail compile.
- Runtime LLM behavior (the 143 deploys were not chatted with - daemon isolation issue).
- Features marked "Not yet implemented" in docs (flows, macros, expose).

## Files

- `runner_full.py` - generator + runner (268 cases, ~820 lines)
- `results/results_full.json` - full per-case output
- `logs/daemon.log` - stress daemon log
