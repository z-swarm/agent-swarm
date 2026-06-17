"""
@module agent_swarm.security.policy
@brief  SecurityPolicy 引擎——DESIGN.md §8.2

W5 范围：
  - ToolRisk 等级（LOW / MEDIUM / HIGH / CRITICAL）
  - 敏感路径黑名单 + 写入路径白名单
  - 命令黑名单（command injection 防御）
  - check_tool() 统一返回 ALLOW / DENY / REQUIRE_APPROVAL

W5 默认策略（Secure by Default）:
  - read_file:  敏感路径 → DENY；其他 ALLOW
  - write_file: 路径不在白名单 → DENY
  - run_command: 命中命令黑名单 → DENY；HIGH 默认 REQUIRE_APPROVAL
  - send_message: LOW（信息泄露面窄）

W6+ 计划:
  - ApprovalFlow 集成 REQUIRE_APPROVAL 决策
  - Approval 实际用飞书/邮件发送卡片
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

log = logging.getLogger(__name__)


class ToolRisk(Enum):
    """
    工具风险等级——DESIGN.md §8.2

    W5 默认绑定（policy 内置，工具实现自己声明等级）：
      LOW       无需审批（read_file / send_message）
      MEDIUM    需路径白名单（write_file）
      HIGH      需沙箱 + 默认审批（run_command）
      CRITICAL  必须审批（外部 API / 网络请求）
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


_DECISION = Literal["ALLOW", "DENY", "REQUIRE_APPROVAL"]


@dataclass
class PolicyDecision:
    """SecurityPolicy.check_tool() 返回值"""

    decision: _DECISION
    reason: str
    auto_sandbox: bool = False  # 是否强制走 SandboxManager（如 run_command）


# ---------------------------------------------------------------------------
# 默认策略常量
# ---------------------------------------------------------------------------

# 敏感路径黑名单（DESIGN §8.2 + W5 实际安全考虑扩展）
SENSITIVE_PATHS: tuple[str, ...] = (
    "/etc/passwd",
    "/etc/shadow",
    "/etc/sudoers",
    "/etc/sudoers.d",
    "~/.ssh/",
    "~/.aws/",
    "~/.azure/",
    "~/.gcp/",
    "~/.kube/",
    "~/.docker/",
    "~/.npmrc",
    "~/.pypirc",
    ".env",
    ".env.local",
    ".env.production",
    "id_rsa",
    "id_ed25519",
    "credentials.json",
    "credentials",
    "secrets",
    "secrets.yaml",
    "secrets.json",
    "/proc/",
    "/sys/",
    "/dev/",
    "shadow",
    "private.key",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
)

# 写入路径白名单模板（占位符 {workspace} 由运行时替换）
WRITABLE_ROOTS: tuple[str, ...] = (
    "{workspace}/",
    "{workspace}/.tmp/",
    "{workspace}/output/",
)

# 命令黑名单（regex 模式）
COMMAND_BLACKLIST: tuple[str, ...] = (
    r"rm\s+-rf\s+/?",  # rm -rf / or rm -rf /
    r"rm\s+-rf\s+/\s*$",  # rm -rf /
    r"mkfs\.",  # 任何 mkfs.X
    r"dd\s+if=.*\s+of=/dev/",  # dd 写裸盘
    r":\(\)\s*\{.*:\|:.*\};:",  # fork bomb
    r"\bcurl\s+.*\|\s*(ba)?sh\b",  # curl | bash 经典攻击
    r"\bwget\s+.*\|\s*(ba)?sh\b",
    r"\bbase64\s+-d\s+.*\|\s*(ba)?sh\b",  # base64 -d | bash
    r"\bsh\s+-c\s+.*rm\s+-rf",  # sh -c 'rm -rf'
    r"\bsudo\s+rm\b",  # sudo rm
)


class SecurityPolicy:
    """
    安全策略引擎——单例

    @note 默认策略采用 Secure by Default：高风险工具一律 REQUIRE_APPROVAL
          通过 set_*_rules() 可放宽 / 收紧（CLI/SDK 暴露）
    """

    def __init__(
        self,
        sensitive_paths: tuple[str, ...] = SENSITIVE_PATHS,
        writable_roots: tuple[str, ...] = WRITABLE_ROOTS,
        command_blacklist: tuple[str, ...] = COMMAND_BLACKLIST,
        workspace: str | None = None,
        require_approval_for: set[str] | None = None,
    ) -> None:
        """
        @param sensitive_paths     路径黑名单（子串匹配）
        @param writable_roots      写入白名单（{workspace} 占位符会被替换）
        @param command_blacklist   命令黑名单（regex）
        @param workspace           {workspace} 替换值
        @param require_approval_for 强制走 REQUIRE_APPROVAL 的工具集合
        """
        self.sensitive_paths = sensitive_paths
        self.writable_roots = tuple(r.replace("{workspace}", workspace) for r in writable_roots) if workspace else writable_roots
        self.command_blacklist = command_blacklist
        self.workspace = workspace
        self.require_approval_for = require_approval_for or set()

    # ------------------------------------------------------------------
    # 工具级 check
    # ------------------------------------------------------------------
    def check_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> PolicyDecision:
        """
        统一入口——根据 tool_name 走对应分支

        @note 检查顺序：require_approval → 路径（path + command 内路径）→
              命令黑名单 → 风险等级
        """
        # 1) 显式 require_approval 列表
        if tool_name in self.require_approval_for:
            return PolicyDecision("REQUIRE_APPROVAL", f"tool {tool_name} requires approval")

        # 2) 路径参数检查（显式 path 字段）
        path = arguments.get("path")
        if isinstance(path, str):
            path_check = self.check_path(path, tool_name)
            if path_check is not None:
                return path_check

        # 3) 命令参数检查（run_command 之类）
        cmd = arguments.get("command")
        if isinstance(cmd, str):
            # 3a) 命令字符串里若含敏感路径——直接 DENY
            path_in_cmd = self._extract_path_from_command(cmd)
            if path_in_cmd is not None:
                path_check = self.check_path(path_in_cmd, tool_name)
                if path_check is not None:
                    return path_check
            # 3b) 命令本身的黑名单
            cmd_check = self.check_command(cmd)
            if cmd_check is not None:
                return cmd_check

        # 4) 风险等级推断
        risk = self._tool_default_risk(tool_name)
        if risk == ToolRisk.CRITICAL:
            return PolicyDecision("REQUIRE_APPROVAL", f"tool {tool_name} is CRITICAL")
        if risk == ToolRisk.HIGH:
            return PolicyDecision(
                "REQUIRE_APPROVAL",
                f"tool {tool_name} is HIGH risk",
                auto_sandbox=True,
            )
        return PolicyDecision("ALLOW", f"tool {tool_name} passed policy")

    # ------------------------------------------------------------------
    # 路径 / 命令级别 check
    # ------------------------------------------------------------------
    def check_path(self, path: str, tool_name: str = "read_file") -> PolicyDecision | None:
        """
        路径白名单/黑名单检查——返回 None 表示通过

        @note 防御深度:
              1) 反斜杠 / 多斜杠 / ./ 全部 normalize 成单 /
              2) 大小写不敏感
              3) 路径前缀绕过：../
        """
        # 1) normalize：反斜杠 + 多斜杠 + ./ 全部清掉
        normalized = path.replace("\\", "/")
        # collapse 多斜杠
        while "//" in normalized:
            normalized = normalized.replace("//", "/")
        # 去掉 ./
        parts: list[str] = []
        for seg in normalized.split("/"):
            if seg == ".":
                continue
            parts.append(seg)
        normalized = "/".join(parts)
        normalized = normalized.lower()

        for sensitive in self.sensitive_paths:
            sensitive_l = sensitive.lower()
            if sensitive_l.startswith("~/"):
                if sensitive_l[2:] in normalized:
                    return PolicyDecision("DENY", f"path matches sensitive {sensitive!r}")
            else:
                if sensitive_l in normalized:
                    return PolicyDecision("DENY", f"path matches sensitive {sensitive!r}")

        # 写入工具：检查是否在白名单
        if tool_name == "write_file" and self.writable_roots:
            for root in self.writable_roots:
                if normalized.startswith(root.lower()):
                    return None
            return PolicyDecision(
                "DENY",
                f"write target {path!r} not in writable roots {list(self.writable_roots)}",
            )
        return None

    def check_command(self, command: str) -> PolicyDecision | None:
        """命令黑名单正则检查——返回 None 表示通过"""
        import re

        cmd_l = command.lower()
        for pattern in self.command_blacklist:
            if re.search(pattern, cmd_l, re.IGNORECASE):
                return PolicyDecision(
                    "DENY",
                    f"command matches blacklist pattern {pattern!r}",
                )
        return None

    @staticmethod
    def _extract_path_from_command(command: str) -> str | None:
        """
        从命令字符串中提取最可疑的路径——供路径黑名单扫描用

        启发式：找第一个看起来像路径的 token
        - 以 / 开头（绝对路径）
        - 以 ./ 或 ../ 开头（相对路径）
        - 包含 ~/.（家目录引用）
        - 包含 /etc/、/var/、/usr/ 等系统路径前缀
        """
        import re

        # 优先匹配家目录和系统绝对路径
        m = re.search(
            r"(?:^|\s|[\"'])([~/][\w./-]+|/etc/[\w./-]+|/var/[\w./-]+|/usr/[\w./-]+|\.\.?/[\w./-]+)",
            command,
        )
        return m.group(1) if m else None

    # ------------------------------------------------------------------
    # 风险等级推断
    # ------------------------------------------------------------------
    @staticmethod
    def _tool_default_risk(tool_name: str) -> ToolRisk:
        """工具默认风险等级——DESIGN §8.2"""
        high_risk = {"run_command", "delete_file", "modify_permissions"}
        critical_risk = {"http_request", "send_email", "shell_exec"}
        medium_risk = {"write_file", "modify_file"}
        if tool_name in critical_risk:
            return ToolRisk.CRITICAL
        if tool_name in high_risk:
            return ToolRisk.HIGH
        if tool_name in medium_risk:
            return ToolRisk.MEDIUM
        return ToolRisk.LOW

    # ------------------------------------------------------------------
    # 规则修改（W6+ 暴露给 CLI）
    # ------------------------------------------------------------------
    def set_sensitive_paths(self, paths: tuple[str, ...]) -> None:
        self.sensitive_paths = paths

    def add_sensitive_path(self, path: str) -> None:
        self.sensitive_paths = (*self.sensitive_paths, path)

    def set_command_blacklist(self, patterns: tuple[str, ...]) -> None:
        self.command_blacklist = patterns

    def add_command_blacklist(self, pattern: str) -> None:
        self.command_blacklist = (*self.command_blacklist, pattern)
