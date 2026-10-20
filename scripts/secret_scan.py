"""Fail if anything that looks like a credential is committed in this project."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "aws access key": re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),
    "aws secret": re.compile(r"(?i)aws_secret_access_key\s*[=:]\s*[A-Za-z0-9/+]{30,}"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "bearer token": re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{30,}"),
    "anthropic key": re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}"),
    "openai key": re.compile(r"\bsk-(proj-)?[A-Za-z0-9]{32,}"),
    "google api key": re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    "github token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    "slack webhook": re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/]+"),
    "discord webhook": re.compile(r"https://(discord|discordapp)\.com/api/webhooks/\d+/[\w-]+"),
    "assigned secret": re.compile(r"(?i)\b(HDR_APPROVAL_SECRET|HDR_ADMIN_TOKEN|AWS_SECRET_ACCESS_KEY)\s*=\s*['\"]?(?!\$)[^\s'\"#$]{12,}"),  # values, not ${VAR} references
}
SKIP_DIRS = {".venv", "node_modules", ".next", "__pycache__", ".pytest_cache", "var", ".git"}
SKIP_SUFFIX = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".sqlite3", ".lock", ".woff", ".woff2"}


def candidate_files() -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "."],
                             cwd=ROOT, capture_output=True, text=True, check=True).stdout.splitlines()
        return [ROOT / p for p in out]
    except (OSError, subprocess.CalledProcessError):
        return [p for p in ROOT.rglob("*") if p.is_file() and not SKIP_DIRS & set(p.relative_to(ROOT).parts)]


def main() -> int:
    hits = []
    files = [f for f in candidate_files() if f.is_file() and f.suffix not in SKIP_SUFFIX
             and not SKIP_DIRS & set(f.relative_to(ROOT).parts)]
    for path in files:
        if path.name == ".env":
            hits.append((path, 0, "a .env file is tracked"))
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for name, pattern in PATTERNS.items():
                if pattern.search(line) and "secret_scan: allow" not in line:
                    hits.append((path, lineno, name))
    for path, lineno, name in hits:
        print(f"{path.relative_to(ROOT)}:{lineno}: possible {name}")
    print(f"secret scan: {len(files)} files, {len(hits)} finding(s)")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
