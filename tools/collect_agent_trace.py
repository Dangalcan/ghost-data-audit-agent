"""Stage the Claude Code session traces of this checkout into agent-trace/ and zip them.

Follows the organizer's agent_trace_instructions.md: session JSONL files (plus their
tool-result folders) go to agent-trace/claude-code/, a readable transcript export sits
next to them, personal memory files and settings are never copied, and the staged text
is redacted before zipping.

    AGENT_TRACE_REDACT="me@example.com,other-secret" python tools/collect_agent_trace.py

Claude Code stores transcripts under %USERPROFILE%/.claude/projects/<encoded project path>/.
Run it after the session has finished writing when possible; review agent-trace/ before
uploading agent-trace.zip.
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
SKIP_DIRS = {"memory"}  # personal notes, not session traces
TEXT_SUFFIXES = {".jsonl", ".json", ".md", ".txt"}
SECRET_RE = re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")


def encoded_project_dirs() -> list[Path]:
    """Claude Code encodes the checkout path by replacing path separators and some
    characters with '-'; match any project folder whose name contains this repo name."""
    if not PROJECTS.exists():
        return []
    keys = {re.sub(r"[^a-z0-9]+", "-", n.lower()) for n in (ROOT.name, ROOT.parent.name)}
    return [p for p in PROJECTS.iterdir() if p.is_dir() and any(k in p.name.lower() for k in keys)]


def redactions() -> list[str]:
    return [s for s in os.environ.get("AGENT_TRACE_REDACT", "").split(",") if s.strip()]


def copy_redacted(src: Path, dest: Path, terms: list[str]) -> int:
    """Copy a file; text files get every redaction term replaced. Returns replacements made."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.suffix not in TEXT_SUFFIXES:
        shutil.copy2(src, dest)
        return 0
    text = src.read_text(encoding="utf-8", errors="replace")
    count = 0
    for term in terms:
        count += text.count(term)
        text = text.replace(term, "[REDACTED]")
    dest.write_text(text, encoding="utf-8", newline="\n")
    return count


def main() -> None:
    stage = ROOT / "agent-trace" / "claude-code"
    if stage.parent.exists():
        shutil.rmtree(stage.parent)
    stage.mkdir(parents=True)
    terms = redactions()
    copied = replaced = 0
    dirs = encoded_project_dirs()
    for d in dirs:
        for f in d.rglob("*"):
            rel = f.relative_to(d)
            if not f.is_file() or any(part in SKIP_DIRS for part in rel.parts):
                continue
            replaced += copy_redacted(f, stage / d.name / rel, terms)
            copied += 1
    for extra, name in ((ROOT / "log.txt", "log.txt"), (ROOT / "chat_transcript.txt", "transcript-export.txt")):
        if extra.exists():
            replaced += copy_redacted(extra, stage / name, terms)
    out = ROOT / "agent-trace.zip"
    leaks = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted((ROOT / "agent-trace").rglob("*")):
            if p.is_file():
                if p.suffix in TEXT_SUFFIXES:
                    leaks += len(SECRET_RE.findall(p.read_text(encoding="utf-8", errors="replace")))
                zf.write(p, p.relative_to(ROOT).as_posix())
    print(
        f"copied {copied} trace files from {len(dirs)} project folder(s); "
        f"{replaced} redactions; {leaks} key-like strings; wrote {out} ({out.stat().st_size / 1e6:.1f} MB)"
    )
    if copied == 0:
        print("No traces found. Check %USERPROFILE%/.claude/projects/ and copy the session JSONL files manually.")
    if leaks:
        print("WARNING: key-like strings found in the staged traces; review before uploading.")


if __name__ == "__main__":
    main()
