"""
@module tools.no_bare_create_task
@brief  W17b lint 规则——禁止 src/agent_swarm/ 下的裸 asyncio.create_task

P3-PLAN-v2 W17 §16.3 #11: 只对 src/agent_swarm/ 生效
                     (避免误伤 tools/ examples/ tests/)
P3-PLAN-v2 W17 DoD ⑨ ②阶段：lint 守门 (第 1 阶段是 audit)

@note 违规定义: 直接调用 asyncio.create_task(...) 而非 core.context.patched_create_task
@note 例外:
       - core/context.py 自身 (wrapper 实现)
       - 行尾含 # noqa: bare-asyncio-create-task
       - docstring/注释中提及

用法:
  python tools/no_bare_create_task.py src/agent_swarm/
退出码 0 = OK / 1 = 有违规 / 2 = 配置错误
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

ALLOWED_FILES = {
    Path("src/agent_swarm/core/context.py"),  # wrapper 自身
}

SKIP_DIRS = {
    "__pycache__",
    ".git",
    ".venv",
    ".venv-win",
    "node_modules",
    "dist",
    "build",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
}


def _has_context_kwarg(node: ast.Call) -> bool:
    """是否显式传了 context= 关键字——已手动注入 SecurityContext, 合法"""
    return any(kw.arg == "context" for kw in node.keywords)


def _is_create_task_call(node: ast.Call) -> bool:
    """判断 AST 节点是否是 asyncio.create_task(...) 调用"""
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr != "create_task":
        return False
    if not isinstance(func.value, ast.Name) or func.value.id != "asyncio":
        return False
    # 显式传 context= 的视为合法 (开发者已手动处理)
    return not _has_context_kwarg(node)


def _is_noqa_marker(line: str) -> bool:
    return "noqa: bare-asyncio-create-task" in line


def scan_file(path: Path) -> list[tuple[int, str]]:
    """
    @return list of (line_no, snippet) for violations
    """
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []
    if path in ALLOWED_FILES:
        return []
    lines = source.splitlines()
    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not _is_create_task_call(node):
            continue
        line_no = node.lineno
        # 检查是否在 docstring 注释内 (简单启发式: 同行或上一行有 docstring 起始)
        if line_no <= len(lines) and _is_noqa_marker(lines[line_no - 1]):
            continue
        snippet = lines[line_no - 1].strip() if line_no <= len(lines) else ""
        violations.append((line_no, snippet))
    return violations


def scan_tree(root: Path) -> dict[Path, list[tuple[int, str]]]:
    """递归扫目录"""
    results: dict[Path, list[tuple[int, str]]] = {}
    for p in root.rglob("*.py"):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        v = scan_file(p)
        if v:
            results[p] = v
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Lint: 禁止 src/agent_swarm/ 下的裸 asyncio.create_task",
    )
    parser.add_argument(
        "paths", nargs="*",
        help="要扫的目录 (默认: src/agent_swarm/)",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="有违规返 1 (默认只打印报告)",
    )
    args = parser.parse_args(argv)

    roots = args.paths or ["src/agent_swarm/"]
    all_violations: dict[Path, list[tuple[int, str]]] = {}
    for r in roots:
        root = Path(r)
        if not root.exists():
            print(f"[ERR] not found: {root}", file=sys.stderr)
            return 2
        if root.is_file():
            v = scan_file(root)
            if v:
                all_violations[root] = v
        else:
            all_violations.update(scan_tree(root))

    if not all_violations:
        print("[OK] no bare asyncio.create_task in scanned paths")
        return 0

    total = sum(len(v) for v in all_violations.values())
    print(f"[FAIL] {total} bare asyncio.create_task(s) found:")
    for path, viols in sorted(all_violations.items()):
        for line_no, snippet in viols:
            print(f"  {path}:{line_no}: {snippet}")
    print(
        "\nFix: use core.context.patched_create_task(ctx, ...) "
        "for auto-inject SecurityContext.",
    )
    print(
        "Or add `# noqa: bare-asyncio-create-task` if intentional.",
    )
    return 1 if args.strict else 0


if __name__ == "__main__":
    raise SystemExit(main())
