"""
@module tools.verify_w9_dod
@brief  W9 DoD 验收脚本——对照 DESIGN §17.2 Phase 2 W3 (MCP 集成)

@usage  .venv/bin/python tools/verify_w9_dod.py
@exit   0 = 全过；非 0 = DoD 未全过
@note   Phase 2 DoD ③ "MCP ≥2 server" 在本脚本的 ② + ⑦ 验证
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))


def _check_registry_from_dict() -> tuple[bool, str]:
    """DoD ②：MCPRegistry.from_dict 可解析 ≥2 server（filesystem + GitHub）"""
    from agent_swarm.mcp import MCPRegistry
    cfg = {
        "filesystem": {
            "transport": "stdio",
            "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
        },
        "github": {
            "transport": "stdio",
            "command": ["npx", "-y", "@modelcontextprotocol/server-github"],
            "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_PERSONAL_ACCESS_TOKEN}"},
        },
    }
    r = MCPRegistry.from_dict(cfg)
    if "filesystem" not in r or "github" not in r:
        return False, f"registry 缺 server: {r.list_names()}"
    fs = r.get("filesystem")
    gh = r.get("github")
    if fs.command[0] != "npx" or "filesystem" not in " ".join(fs.command):
        return False, f"filesystem command 错: {fs.command}"
    if "github" not in " ".join(gh.command):
        return False, f"github command 错: {gh.command}"
    if gh.env.get("GITHUB_PERSONAL_ACCESS_TOKEN") != "${GITHUB_PERSONAL_ACCESS_TOKEN}":
        return False, f"github token 应为 SecretManager 引用: {gh.env}"
    return True, f"  2 server: {r.list_names()}\n  github token 走 SecretManager 引用 ✓"


def _check_stdio_protocol() -> tuple[bool, str]:
    """DoD ③：StdioMCPClient JSON-RPC 2.0 协议 + 错误处理"""
    proc = subprocess.run(
        [".venv/bin/python", "-m", "pytest", "tests/unit/test_mcp_stdio.py", "-q"],
        cwd=REPO, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return False, f"stdio 测试失败: {proc.stdout[-300:]}"
    # 提取 passed 数
    last = proc.stdout.strip().splitlines()[-1]
    return True, f"  {last}"


def _check_adapter() -> tuple[bool, str]:
    """DoD ④ ⑤：MCPToolAdapter + await_build_tool_adapters"""
    proc = subprocess.run(
        [".venv/bin/python", "-m", "pytest", "tests/unit/test_mcp_adapter.py", "-q"],
        cwd=REPO, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return False, f"adapter 测试失败: {proc.stdout[-300:]}"
    last = proc.stdout.strip().splitlines()[-1]
    return True, f"  {last}"


def _check_e2e_two_servers() -> tuple[bool, str]:
    """DoD ⑦：Phase 2 DoD ③ ≥2 server——e2e 用 mock 验证"""
    proc = subprocess.run(
        [".venv/bin/python", "-m", "pytest", "tests/e2e/test_w9_mcp_e2e.py", "-q"],
        cwd=REPO, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return False, f"e2e 失败: {proc.stdout[-300:]}"
    last = proc.stdout.strip().splitlines()[-1]
    return True, f"  {last}"


def _check_w9_example() -> tuple[bool, str]:
    """DoD ⑥：examples/w9_mcp_github_filesystem.yaml 合法 + MCPRegistry 可消费"""
    import yaml
    from agent_swarm.mcp import MCPRegistry
    p = REPO / "examples" / "w9_mcp_github_filesystem.yaml"
    if not p.exists():
        return False, "examples/w9_mcp_github_filesystem.yaml 不存在"
    with open(p, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if "mcp_servers" not in cfg:
        return False, "YAML 缺 mcp_servers 字段"
    r = MCPRegistry.from_dict(cfg["mcp_servers"])
    if "filesystem" not in r or "github" not in r:
        return False, f"server 注册不全: {r.list_names()}"
    return True, f"  YAML 合法 + 2 server 可消费: {r.list_names()}"


def _check_readme() -> tuple[bool, str]:
    """DoD ⑧：README quickstart + 状态表含 W9"""
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    flags = {
        "w9_mcp_github_filesystem.yaml": "w9_mcp_github_filesystem.yaml" in readme,
        "MCPRegistry 引用": "MCPRegistry" in readme,
        "W9 状态行": "**W9**" in readme,
    }
    missing = [k for k, v in flags.items() if not v]
    if missing:
        return False, f"README 缺: {missing}"
    return True, "  quickstart + 状态表齐全"


def main() -> int:
    test_files = [
        "tests/unit/test_mcp_registry.py",
        "tests/unit/test_mcp_stdio.py",
        "tests/unit/test_mcp_adapter.py",
        "tests/e2e/test_w9_mcp_e2e.py",
    ]
    result = subprocess.run(
        [".venv/bin/python", "-m", "pytest", *test_files, "-q"],
        cwd=REPO, capture_output=True, text=True,
    )
    check1_ok = result.returncode == 0
    check1_evidence = result.stdout.strip().splitlines()[-1] if result.stdout else (result.stderr or "")

    checks: list[tuple[str, bool, str]] = [
        ("① 4 个 W9 测试文件 pytest 全过", check1_ok, check1_evidence),
        ("② MCPRegistry.from_dict 解析 ≥2 server", *_check_registry_from_dict()),
        ("③ StdioMCPClient JSON-RPC 2.0 协议", *_check_stdio_protocol()),
        ("④ MCPToolAdapter 包装 + name 加前缀", *_check_adapter()),
        ("⑤ await_build_tool_adapters 异步工厂", *_check_adapter()),
        ("⑥ examples/w9_mcp_github_filesystem.yaml", *_check_w9_example()),
        ("⑦ Phase 2 DoD ③: ≥2 server e2e 走通", *_check_e2e_two_servers()),
        ("⑧ README quickstart + 状态表", *_check_readme()),
    ]

    print("=" * 72)
    print(" W9 DoD 验收报告 (DESIGN §17.2 Phase 2 W3)")
    print("=" * 72)
    for name, ok, evidence in checks:
        icon = "✅" if ok else "❌"
        print(f"  {icon} {name}")
        if evidence:
            for line in evidence.splitlines()[:6]:
                print(f"     {line}")
    print("=" * 72)
    passed = sum(1 for _, ok, _ in checks if ok)
    total = len(checks)
    if passed == total:
        print(f" 总计: {passed}/{total} 通过")
        print(" ✅ W9 DoD 全部通过 → 阶段门控 → Phase 2 DoD ③ 达成")
        return 0
    print(f" 总计: {passed}/{total} 通过")
    print(" ❌ W9 DoD 未全过 → 阶段门控失败 → 停手回头修")
    return 1


if __name__ == "__main__":
    sys.exit(main())
