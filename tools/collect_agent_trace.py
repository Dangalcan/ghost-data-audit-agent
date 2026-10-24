"""Stage Claude Code session traces for this checkout into agent-trace/ and zip them.

Claude Code stores transcripts under %USERPROFILE%/.claude/projects/<encoded project path>/.
Run this AFTER quitting the Claude Code session(s) so the JSONL files are complete:

    python tools/collect_agent_trace.py

Then upload agent-trace.zip with the submission. Review the staged files first; they
contain prompts, tool calls and local paths (no credentials are copied).
"""

from __future__ import annotations

import os
import re
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOME = Path(os.environ.get("USERPROFILE") or Path.home())
PROJECTS = HOME / ".claude" / "projects"


def encoded_project_dirs() -> list[Path]:
    """Claude Code encodes the checkout path by replacing path separators and some
    characters with '-'; match any project folder whose name contains this repo name."""
    if not PROJECTS.exists():
        return []
    keys = {re.sub(r"[^a-z0-9]+", "-", n.lower()) for n in (ROOT.name, ROOT.parent.name)}
    return [p for p in PROJECTS.iterdir() if p.is_dir() and any(k in p.name.lower() for k in keys)]


def main() -> None:
    stage = ROOT / "agent-trace" / "claude-code"
    if stage.parent.exists():
        shutil.rmtree(stage.parent)
    stage.mkdir(parents=True)
    copied = 0
    for d in encoded_project_dirs():
        for f in d.rglob("*"):
            if f.is_file() and f.suffix in (".jsonl", ".json", ".md", ".txt"):
                rel = f.relative_to(d)
                dest = stage / d.name / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dest)
                copied += 1
    log = ROOT / "log.txt"
    if log.exists():
        shutil.copy2(log, stage / "log.txt")
    out = ROOT / "agent-trace.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted((ROOT / "agent-trace").rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(ROOT).as_posix())
    print(f"copied {copied} trace files from {len(encoded_project_dirs())} project folder(s); wrote {out}")
    if copied == 0:
        print("No traces found. Check %USERPROFILE%/.claude/projects/ and copy the session JSONL files manually.")


if __name__ == "__main__":
    main()
