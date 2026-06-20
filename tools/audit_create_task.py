"""
@file tools.audit_create_task
@brief  W17a tool: scan src/ for bare asyncio.create_task usage

DESIGN §16.3 #11 + P3-PLAN-v2 W17 DoD 8 W17a:
  "Audit existing asyncio.create_task call sites and migrate to context= form"

Rules:
  - Scan src/agent_swarm/**/*.py
  - Detect bare asyncio.create_task(coro) calls (no context= kwarg)
  - Multi-line: check 3 lines ahead for context= keyword
  - Skip docstrings (3-quote state tracking)
  - Skip patched_create_task / asyncio.create_task inside the wrapper itself
  - Recommend: from agent_swarm.core.context import patched_create_task

Usage:
    python tools/audit_create_task.py
    python tools/audit_create_task.py --strict  # include comments
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO / "src"

# Detection: asyncio.create_task( ... 3 lines ... )
BARE_PATTERN = re.compile(r"\basyncio\.create_task\s*\(")
COMMENT_LINE = re.compile(r"^\s*#")
TRIPLE_QUOTE = re.compile('"""')

# Files where the bare call is legitimate (the wrapper itself + tests)
EXCLUDE_FILES = (
    "src/agent_swarm/core/context.py",  # the wrapper itself
)


def scan(strict: bool = False) -> tuple[int, list[str]]:
    """
    Scan src/agent_swarm/ — returns (violation_count, issue_messages)
    """
    issues: list[str] = []
    count = 0
    if not SRC_ROOT.exists():
        return count, issues
    for path in SRC_ROOT.rglob("*.py"):
        rel = str(path.relative_to(REPO)).replace("\\", "/")
        if rel in EXCLUDE_FILES:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        lines = content.splitlines()
        in_triple_quote = False
        for line_no, line in enumerate(lines, start=1):
            if not strict and COMMENT_LINE.match(line):
                continue
            # Triple-quote state tracking
            if '"""' in line:
                quote_count = line.count('"""')
                in_triple_quote = (quote_count % 2 == 1) ^ in_triple_quote
                if in_triple_quote and not BARE_PATTERN.search(line):
                    continue
            if in_triple_quote:
                continue
            if not BARE_PATTERN.search(line):
                continue
            # Skip if this line uses patched_create_task
            if "patched_create_task" in line:
                continue
            # Skip if context= keyword appears in the same line OR next 3 lines
            # (multi-line call): asyncio.create_task(\n  coro,\n  context=ctx)
            context_window = "\n".join(lines[line_no - 1:line_no + 3])
            if "context=" in context_window:
                continue
            count += 1
            issues.append(
                f"  {rel}:{line_no}: "
                f"bare asyncio.create_task() - use patched_create_task() "
                f"from agent_swarm.core.context instead\n    {line.strip()[:100]}"
            )
    return count, issues


def main() -> int:
    parser = argparse.ArgumentParser(
        description="W17a audit: scan bare asyncio.create_task usage (DESIGN §16.3 #11)"
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="Strict mode: comments count too",
    )
    parser.add_argument(
        "--quiet", action="store_true", help="Only output violation count",
    )
    args = parser.parse_args()

    count, issues = scan(strict=args.strict)
    if args.quiet:
        print(count)
        return 0 if count == 0 else 1

    if count == 0:
        print("[audit_create_task] OK: no bare asyncio.create_task() found")
        return 0

    print(f"[audit_create_task] FAIL: {count} bare asyncio.create_task() call(s):")
    for issue in issues:
        print(issue)
    print(
        "\nFix: replace asyncio.create_task(coro) with "
        "patched_create_task(coro) - auto-injects SecurityContext."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())

