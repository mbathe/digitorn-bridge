from __future__ import annotations

import os

from digitorn.core.runtime.types import WORKSPACE_PLACEHOLDER
from digitorn.modules.shell.module import ShellModule


class TestShellPromptEnvironment:

    def test_prompt_section_describes_execution_environment(self):
        module = ShellModule()

        sections = module.get_prompt_sections()

        assert len(sections) == 1
        section = sections[0]
        assert section["title"] == "Execution Environment"
        assert "Shell executable for shell.bash" in section["content"]
        assert "Shell command dialect" in section["content"]
        assert f"Session workspace root: {WORKSPACE_PLACEHOLDER}" in section["content"]
        assert "daemon process directory" in section["content"]
        assert f"Path separator: {os.sep}" in section["content"]
        assert "Use filesystem tools for file reads/edits/search" in section["content"]

    def test_context_snippet_includes_shell_dialect(self):
        module = ShellModule()
        snippet = module.get_context_snippet()

        assert snippet is not None
        assert "dialect=" in snippet
        assert f"Session workspace: {WORKSPACE_PLACEHOLDER}" in snippet
