"""Compile all 35 channels example YAML files.

Verifies that every example passes the full compilation pipeline:
YAML -> AppDefinition -> variable resolution -> module validation -> CompiledApp.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from digitorn.core.app import AppYAMLCompiler
from digitorn.modules.registry import ModuleRegistry
from digitorn.core.loader import load_modules


EXAMPLES_DIR = Path(__file__).parent.parent / "examples" / "channels"


@pytest.fixture(scope="module")
def compiler() -> AppYAMLCompiler:
    """Create a compiler with all modules loaded."""
    registry = ModuleRegistry()
    load_modules(registry)
    return AppYAMLCompiler(registry)


def _example_files() -> list[Path]:
    if not EXAMPLES_DIR.is_dir():
        return []
    return sorted(EXAMPLES_DIR.glob("*.yaml"))


# Dummy secrets/env so {{secret.*}} and {{env.*}} don't fail
_DUMMY_SECRETS = {
    "DEEPSEEK_API_KEY": "sk-test-dummy",
    "GH_WEBHOOK_SECRET": "test-secret",
    "SLACK_WEBHOOK_URL": "https://hooks.slack.com/test",
    "SLACK_OPS_WEBHOOK": "https://hooks.slack.com/test",
    "TWILIO_WA_SECRET": "test-twilio",
    "TWILIO_AUTH_BASE64": "dGVzdA==",
    "EMAIL_APP_PASSWORD": "test-password",
    "DB_URL": "sqlite:///test.db",
    "EMAIL_PWD": "test-pwd",
    "GMAIL_APP_PASSWORD": "test-gmail-pwd",
    "TELEGRAM_BOT_TOKEN": "123456:ABC-test",
    "DISCORD_BOT_TOKEN": "test-discord-token",
    "SLACK_BOT_TOKEN": "xoxb-test",
    "SLACK_APP_TOKEN": "xapp-test",
}

_DUMMY_ENV = {
    "DEEPSEEK_API_KEY": "sk-test-dummy",
    "SUPPORT_EMAIL": "test@example.com",
    "REPORT_EMAIL": "report@example.com",
    "TWILIO_SID": "AC_test",
    "WEBHOOK_SECRET": "test-webhook-secret",
    "GMAIL_ADDRESS": "test@gmail.com",
}


@pytest.fixture(autouse=True)
def _inject_env():
    """Inject dummy env vars for compilation, restore after."""
    saved = {}
    for k, v in _DUMMY_ENV.items():
        saved[k] = os.environ.get(k)
        os.environ[k] = v
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.mark.parametrize(
    "yaml_file",
    _example_files(),
    ids=lambda p: p.stem,
)
def test_compile_example(compiler: AppYAMLCompiler, yaml_file: Path):
    """Each example YAML compiles without errors."""
    compiled = compiler.compile_file(yaml_file, secrets=_DUMMY_SECRETS)

    assert compiled.app_id, f"No app_id in {yaml_file.name}"
    assert compiled.agents, f"No agents in {yaml_file.name}"
    assert compiled.execution.mode == "background"
    assert "channels" in compiled.module_ids

    ch_config = compiled.modules.get("channels")
    assert ch_config is not None
    providers = ch_config.config.get("providers", {})
    assert len(providers) > 0, f"No providers in {yaml_file.name}"


class TestCompilationDetails:
    def test_cron_schedule_preserved(self, compiler: AppYAMLCompiler):
        compiled = compiler.compile_file(EXAMPLES_DIR / "01-cron-hello.yaml", secrets=_DUMMY_SECRETS)
        providers = compiled.modules["channels"].config["providers"]
        # Find cron provider
        cron_prov = next(
            (p for p in providers.values() if isinstance(p, dict) and p.get("adapter") == "cron"),
            None,
        )
        assert cron_prov is not None
        assert cron_prov["config"]["schedule"] == "* * * * *"

    def test_webhook_inbound_path(self, compiler: AppYAMLCompiler):
        compiled = compiler.compile_file(EXAMPLES_DIR / "05-webhook-inbound.yaml", secrets=_DUMMY_SECRETS)
        providers = compiled.modules["channels"].config["providers"]
        webhook_prov = next(
            (p for p in providers.values() if isinstance(p, dict) and p.get("adapter") == "webhook"),
            None,
        )
        assert webhook_prov is not None
        assert webhook_prov["config"].get("inbound_path")

    def test_runtime_vars_preserved(self, compiler: AppYAMLCompiler):
        """{{event.payload.*}} expressions survive compilation."""
        compiled = compiler.compile_file(EXAMPLES_DIR / "03-file-watcher-basic.yaml", secrets=_DUMMY_SECRETS)
        providers = compiled.modules["channels"].config["providers"]
        for prov in providers.values():
            if not isinstance(prov, dict):
                continue
            act = prov.get("activation", {})
            msg = act.get("message", "") if isinstance(act, dict) else ""
            if "event.payload" in msg:
                assert "{{event.payload" in msg, f"Runtime var was resolved at compile time: {msg}"
                return
        pytest.skip("No event.payload template in this example")

    def test_full_pipeline_compiles(self, compiler: AppYAMLCompiler):
        """Example 33 with filter+prepare+route compiles."""
        compiled = compiler.compile_file(EXAMPLES_DIR / "33-full-pipeline.yaml", secrets=_DUMMY_SECRETS)
        providers = compiled.modules["channels"].config["providers"]
        tickets = providers.get("tickets", {})
        assert tickets.get("adapter") == "webhook"
        act = tickets.get("activation", {})
        assert len(act.get("filter", [])) > 0
        assert len(act.get("prepare", [])) > 0
        assert act.get("route") is not None

    def test_multi_agent_example(self, compiler: AppYAMLCompiler):
        """Example 32 with 3 agents compiles."""
        compiled = compiler.compile_file(EXAMPLES_DIR / "32-three-triggers-three-agents.yaml", secrets=_DUMMY_SECRETS)
        assert len(compiled.agents) >= 3
        assert "channels" in compiled.module_ids

    def test_capabilities_preserved(self, compiler: AppYAMLCompiler):
        """Example 31 with capabilities compiles with security profile."""
        compiled = compiler.compile_file(EXAMPLES_DIR / "31-cron-capabilities.yaml", secrets=_DUMMY_SECRETS)
        assert compiled.security_profile is not None
