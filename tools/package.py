"""Build code.zip for submission (README.md + code/, no dataset, venvs or caches of bytecode).

Usage: python tools/package.py
"""
from __future__ import annotations

import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", ".venv", "venv", "node_modules", ".git"}
EXCLUDE_SUFFIXES = {".pyc", ".zip", ".tar", ".gz", ".tgz", ".7z", ".rar"}


def main() -> None:
    out = ROOT / "code.zip"
    if out.exists():
        out.unlink()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(ROOT / "README.md", "README.md")
        zf.write(ROOT / "requirements.txt", "requirements.txt")
        zf.write(ROOT / ".env.example", ".env.example")
        for p in sorted((ROOT / "code").rglob("*")):
            if p.is_dir() or any(part in EXCLUDE_DIRS for part in p.parts) or p.suffix in EXCLUDE_SUFFIXES:
                continue
            zf.write(p, p.relative_to(ROOT).as_posix())
    names = zipfile.ZipFile(out).namelist()
    print(f"{out} written: {len(names)} files, {out.stat().st_size / 1024:.0f} KB")
    for required in ("code/main.py", "code/evaluation/evaluation_report.md", "code/evaluation/usage_report.md", "README.md"):
        print(("ok  " if required in names else "MISSING ") + required)


if __name__ == "__main__":
    main()
