"""
@module agent_swarm.tools.builtin.file_ops
@brief  W1 内置工具：read_file（含基础敏感路径黑名单）

DESIGN.md §8.2 SecurityPolicy 完整版在 W5 落地；W1 先内置最小防护
确保 W5 启用 SecurityPolicy 时这些规则会被覆盖（向上兼容）
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# W1 最小敏感路径黑名单——W5 SecurityPolicy 会扩展并接管
_SENSITIVE_FRAGMENTS = (
    "/etc/passwd",
    "/etc/shadow",
    "/.ssh/",
    "/.aws/",
    "/proc/",
    "/sys/",
    ".env",
    "credentials",
    "secrets",
)

# 单次读取行数上限（防 token 爆炸——DESIGN.md §9.3 limit_tool_result）
DEFAULT_MAX_LINES = 500


class ReadFileError(Exception):
    """读取文件失败的统一异常"""


class ReadFileTool:
    """
    读取本地文件——W1 唯一工具

    @note 出错时返回带 [error] 前缀的字符串，而非抛异常——
          这样 LLM 能感知错误并自我修正，而不是中断 agent loop。
    """

    name = "read_file"
    description = "读取本地文件内容。返回前 N 行；超长则末尾标注被截断。"
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要读取的文件路径（支持相对当前工作目录）",
            },
            "max_lines": {
                "type": "integer",
                "description": f"最多读取行数，默认 {DEFAULT_MAX_LINES}",
                "default": DEFAULT_MAX_LINES,
            },
        },
        "required": ["path"],
    }

    def __init__(self, workspace: Path | str | None = None) -> None:
        """
        @param workspace 可选限制根目录——读取必须落在此目录内
                         W1 默认为当前工作目录；W5 由 SecurityContext 注入
        """
        self.workspace = Path(workspace).resolve() if workspace else Path.cwd().resolve()

    async def invoke(self, arguments: dict[str, Any]) -> str:
        """执行工具——返回 LLM 可消化的字符串"""
        path_arg = arguments.get("path")
        if not path_arg or not isinstance(path_arg, str):
            return "[error] missing or invalid 'path' argument"

        max_lines = arguments.get("max_lines", DEFAULT_MAX_LINES)
        try:
            max_lines = int(max_lines)
        except (TypeError, ValueError):
            max_lines = DEFAULT_MAX_LINES

        try:
            return self._read(path_arg, max_lines)
        except ReadFileError as exc:
            return f"[error] {exc}"
        except OSError as exc:
            return f"[error] OS error: {exc}"

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _read(self, path: str, max_lines: int) -> str:
        # 1) 敏感路径检测——简单字符串包含足以挡住 LLM 试探
        normalized = path.replace("\\", "/").lower()
        for frag in _SENSITIVE_FRAGMENTS:
            if frag in normalized:
                raise ReadFileError(
                    f"sensitive path blocked: {frag!r} matches {path!r}"
                )

        # 2) 解析为绝对路径（resolve 处理符号链接 + ../）
        target = Path(path).expanduser()
        if not target.is_absolute():
            target = self.workspace / target

        try:
            target = target.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ReadFileError(f"file not found: {path}") from exc

        # 3) 必须在 workspace 内（防 path traversal）
        try:
            target.relative_to(self.workspace)
        except ValueError as exc:
            raise ReadFileError(
                f"path outside workspace: {target} not under {self.workspace}"
            ) from exc

        # 4) 必须是普通文件
        if not target.is_file():
            raise ReadFileError(f"not a regular file: {target}")

        # 5) 读取（按行截断）
        lines: list[str] = []
        truncated = False
        with open(target, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= max_lines:
                    truncated = True
                    break
                lines.append(line.rstrip("\n"))

        body = "\n".join(lines)
        rel = os.path.relpath(target, self.workspace)
        header = f"# {rel} ({len(lines)} lines{', truncated' if truncated else ''})\n"
        return header + body
