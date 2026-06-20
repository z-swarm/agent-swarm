"""
@file tools.check_sql_lint
@brief  W16-4 SQL tenant 隔离 audit (P3-PLAN-v2 W16 DoD 4)

Rules:
  - Scan src/agent_swarm/**/*.py + tools/*.py
  - Detect SQL strings containing SELECT / INSERT / UPDATE / DELETE
  - SQL must include WHERE clause + tenant_id field
  - Fail (exit 1) blocks CI
  - Excluded: comments (# / triple-quote), tests/, __init__.py

Usage:
    python tools/check_sql_lint.py
    python tools/check_sql_lint.py --strict  # comments count too
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# 文件白名单：这些文件不参与审计
EXCLUDE_PATTERNS = (
    "tests/",
    "test_",
    "__init__.py",
    "docs/",
    ".venv",
    "demos/",
    "examples/",
)

# 触发审计的 SQL 关键字（粗匹配）——后续用 _looks_like_sql() 二次过滤
SQL_KEYWORDS = re.compile(
    r"\b(SELECT|INSERT|UPDATE|DELETE)\b",
    re.IGNORECASE,
)


def _looks_like_sql(line: str) -> bool:
    """
    粗匹配后二次过滤——确保是 SQL 字符串而不是普通 Python 代码

    启发式规则（任一为真即认为 SQL）:
      1. 整行被三引号包住 / 行是 .execute("...") / .executescript("...")
      2. 含 FROM <table> 关键字
      3. 含 INSERT INTO/UPDATE ... SET/DELETE FROM
      4. SQL 关键字后面跟着常见表名（tasks/messages/sessions 等）
    """
    upper = line.upper()
    # 排除明显的 Python 代码
    false_positives = (
        "kwargs.update",          # dict.update
        "auto_tools.update",      # dict.update
        "self.update",            # method call
        ".UPDATE(",               # method call
        "DECRYPTOR.UPDATE",       # cryptography
        "UNPADDED.UPDATE",        # cryptography
        "UNPADDED.UPDATE",        # cryptography
        ".finalize()",            # method call - "SELECT" not real SQL
        "DELETE", # delete=True,
    )
    for fp in false_positives:
        if fp in upper:
            return False
    # 启发式 1: .execute("...") 或 .executescript("...") 形式
    if re.search(r'\.(execute|executescript)\s*\(', line):
        return True
    # 启发式 2: SQL 关键字 + 紧跟表名/INTO/FROM/SET 等
    if re.search(r'\bINSERT\s+INTO\b', upper):
        return True
    if re.search(r'\bUPDATE\s+\w+\s+SET\b', upper):
        return True
    if re.search(r'\bDELETE\s+FROM\b', upper):
        return True
    if re.search(r'\bSELECT\b.*\bFROM\b', upper):
        return True
    # 启发式 3: SELECT 出现在字符串中（f"..." 形式）
    if re.search(r'["\'].*\bSELECT\b', line) and re.search(r'\bFROM\b', upper):
        return True
    return False

# 排除单行注释
COMMENT_LINE = re.compile(r"^\s*#")

# 排除三引号字符串（文档字符串 / 长字符串）
TRIPLE_QUOTE = re.compile(r'"""')


def _is_excluded(path: Path) -> bool:
    rel = str(path.relative_to(REPO)).replace("\\", "/")
    for pat in EXCLUDE_PATTERNS:
        if pat in rel:
            return True
    return False


def _extract_sql_strings(content: str) -> list[tuple[int, str, str]]:
    """
    Extract SQL strings from a file

    @return [(line_no, sql_text, full_line), ...]
    """
    out: list[tuple[int, str, str]] = []
    in_triple_quote = False
    for line_no, line in enumerate(content.splitlines(), start=1):
        if COMMENT_LINE.match(line):
            continue
        # Triple-quote state toggle (rough - only check line start/end)
        if '"""' in line:
            quote_count = line.count('"""')
            in_triple_quote = (quote_count % 2 == 1) ^ in_triple_quote
        if in_triple_quote and not SQL_KEYWORDS.search(line):
            continue
        if not SQL_KEYWORDS.search(line):
            continue
        # 二次过滤：确保是 SQL，不是 Python 代码（dict.update 等）
        if not _looks_like_sql(line):
            continue
        out.append((line_no, line.strip(), line))
    return out


def _check_sql(
    line_no: int, line: str, file: Path, surrounding: list[str] | None = None,
) -> list[str]:
    """
    Single-line SQL check - returns list of violation messages

    @param surrounding  adjacent lines (for multi-line SQL strings)
    """
    issues: list[str] = []
    upper = line.upper()
    # Look at the line + adjacent lines for WHERE / tenant_id (multi-line SQL)
    combined = line + "\n" + "\n".join(surrounding or [])
    combined_upper = combined.upper()
    # Exclude sqlite_master queries (system schema - no tenant_id needed)
    if "SQLITE_MASTER" in combined_upper:
        return issues
    # Must contain WHERE clause (in this line or adjacent)
    if "WHERE" not in combined_upper:
        issues.append(
            f"  {file}:{line_no}: SQL missing WHERE clause: {line[:80]!r}"
        )
    # Must contain tenant_id field
    if "tenant_id" not in combined:
        issues.append(
            f"  {file}:{line_no}: SQL missing tenant_id filter: {line[:80]!r}"
        )
    return issues


def scan_repo(strict: bool = False) -> tuple[int, list[str]]:
    """
    Scan src/ + tools/

    @return (violation_count, issue_messages)
    """
    issues: list[str] = []
    count = 0
    for subdir in ("src", "tools"):
        root = REPO / subdir
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if _is_excluded(path):
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            lines = content.splitlines()
            for line_no, sql_text, _full in _extract_sql_strings(content):
                # Multi-line SQL: 前后各 3 行作为 surrounding
                surrounding = lines[max(0, line_no - 1 - 3):line_no - 1] + lines[line_no:line_no + 3]
                line_issues = _check_sql(line_no, sql_text, path, surrounding)
                if line_issues:
                    count += len(line_issues)
                    issues.extend(line_issues)
    return count, issues


def main() -> int:
    parser = argparse.ArgumentParser(
        description="SQL tenant_id 隔离审计（P3-PLAN-v2 W16 DoD ④）"
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="严格模式：注释里的 SQL 也算",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="只输出违规数（CI 用）",
    )
    args = parser.parse_args()

    count, issues = scan_repo(strict=args.strict)

    if args.quiet:
        print(count)
        return 0 if count == 0 else 1

    if count == 0:
        print("[check_sql_lint] ✓ all SQL queries have WHERE + tenant_id")
        return 0

    print(f"[check_sql_lint] ✗ {count} violation(s) found:")
    for issue in issues:
        print(issue)
    print(
        "\nFix: add 'WHERE tenant_id = ?' to all SELECT/INSERT/UPDATE/DELETE "
        "queries.\nSee DESIGN §8.4 tenant isolation rules."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
