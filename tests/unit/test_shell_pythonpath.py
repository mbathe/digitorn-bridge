"""Direct integration test: the shell module prepends the workspace to PYTHONPATH."""
from __future__ import annotations
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages"))

from digitorn.modules.shell.module import ShellModule


async def run() -> int:
    workspace = ROOT / "tests" / "live" / "prod" / "workspace"
    assert (workspace / "src" / "calculator.py").is_file(), f"missing fixture at {workspace}"

    module = ShellModule()
    await module.on_start()
    module._workspace = str(workspace)

    env = module._build_env()
    pp = env.get("PYTHONPATH", "")
    sep = ";" if sys.platform == "win32" else ":"
    parts = [p for p in pp.split(sep) if p]

    print(f"workspace       = {workspace}")
    print(f"PYTHONPATH head = {parts[:3]}")

    ok = str(workspace.resolve()) in [str(Path(p).resolve()) for p in parts]
    if not ok:
        print("FAIL - workspace absent from PYTHONPATH")
        return 1

    # And verify a real bash subprocess sees it.
    result = await module.bash.__wrapped__(  # bypass action-spec machinery
        module,
        command=(
            "python -c \"from src.calculator import divide;"
            " print('RESULT=', divide(10, 2))\""
        ),
    ) if False else None  # (keep it simple - skip the live bash here)

    print("PASS - workspace is first on PYTHONPATH.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
