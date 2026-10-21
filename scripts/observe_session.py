#!/usr/bin/env python3
"""Thin launcher from the TBBCC plugin to the HTLsAgentsChat observation CLI.

The ME/AR mechanism lives in the sibling ``HTLsAgentsChat`` repository; TBBCC
only ships the slash command and the behaviour recorder.  This wrapper finds
the sibling (or ``AGENTIC_CHAT_ROOT``), then runs
``python -m agentic_local observe <args>`` with the correct module path.

It never captures credentials and never starts a model by itself.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _chat_root() -> Path:
    env = os.environ.get("AGENTIC_CHAT_ROOT")
    candidates = [Path(env)] if env else []
    candidates += [
        PLUGIN_ROOT.parent / "HTLsAgentsChat-main",
        PLUGIN_ROOT.parent / "HTLsAgentsChat",
    ]
    parent = PLUGIN_ROOT.parent
    for _ in range(3):
        candidates.append(parent / "HTLsAgentsChat-main")
        candidates.append(parent / "HTLsAgentsChat")
        parent = parent.parent
    for candidate in candidates:
        if (candidate / "scripts" / "agentic_local" / "cli.py").exists():
            return candidate.resolve()
    raise SystemExit(
        "HTLsAgentsChat not found; clone it beside TBBCC or set AGENTIC_CHAT_ROOT")


def main(argv: list[str]) -> int:
    chat = _chat_root()
    scripts = chat / "scripts"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(scripts) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env.setdefault("TBBCC_ROOT", str(PLUGIN_ROOT))
    # ``cwd`` below is the mechanism checkout; remember where the human session
    # was actually started so the observation marker lands in its project root.
    env.setdefault("AGENTIC_SESSION_DIR", os.getcwd())
    command = [sys.executable, "-m", "agentic_local", "observe", *argv]
    return subprocess.call(command, cwd=str(scripts), env=env)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
