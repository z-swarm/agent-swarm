"""单元测试：read_file 工具——含敏感路径黑名单与越界保护"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_swarm.tools.builtin.file_ops import ReadFileTool


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """构造一个工作目录，含若干测试文件"""
    (tmp_path / "hello.txt").write_text("hello\nworld\n", encoding="utf-8")
    big = "\n".join(f"line{i}" for i in range(1000))
    (tmp_path / "big.txt").write_text(big, encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "nested.txt").write_text("nested-content\n", encoding="utf-8")
    return tmp_path


async def test_read_file_basic(workspace: Path) -> None:
    """常规读取：返回内容 + header"""
    tool = ReadFileTool(workspace=workspace)
    out = await tool.invoke({"path": "hello.txt"})
    assert "hello" in out
    assert "world" in out
    assert "hello.txt" in out


async def test_read_file_relative_path(workspace: Path) -> None:
    """子目录相对路径"""
    tool = ReadFileTool(workspace=workspace)
    out = await tool.invoke({"path": "sub/nested.txt"})
    assert "nested-content" in out


async def test_read_file_missing(workspace: Path) -> None:
    """不存在的文件返回 [error] 而非抛异常"""
    tool = ReadFileTool(workspace=workspace)
    out = await tool.invoke({"path": "nonexistent.txt"})
    assert out.startswith("[error]")
    assert "not found" in out.lower()


async def test_read_file_truncates_at_max_lines(workspace: Path) -> None:
    """超长文件按 max_lines 截断 + 标注"""
    tool = ReadFileTool(workspace=workspace)
    out = await tool.invoke({"path": "big.txt", "max_lines": 50})
    assert "truncated" in out
    # 第 50 行存在，第 60 行不应在
    assert "line49" in out
    assert "line60" not in out


async def test_read_file_blocks_sensitive_path(workspace: Path) -> None:
    """敏感路径黑名单生效"""
    tool = ReadFileTool(workspace=workspace)
    out = await tool.invoke({"path": "/etc/passwd"})
    assert out.startswith("[error]")
    assert "sensitive" in out.lower()


async def test_read_file_blocks_dotenv(workspace: Path) -> None:
    """.env 也被拦截"""
    tool = ReadFileTool(workspace=workspace)
    out = await tool.invoke({"path": ".env"})
    assert out.startswith("[error]")


async def test_read_file_blocks_path_traversal(workspace: Path, tmp_path: Path) -> None:
    """通过 ../ 越出 workspace 应被拦截"""
    # 在 workspace 之外写一个文件
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    try:
        tool = ReadFileTool(workspace=workspace)
        out = await tool.invoke({"path": "../outside.txt"})
        assert out.startswith("[error]")
        assert "outside workspace" in out.lower()
    finally:
        outside.unlink(missing_ok=True)


async def test_read_file_invalid_args(workspace: Path) -> None:
    """缺 path 参数返回 [error]"""
    tool = ReadFileTool(workspace=workspace)
    out = await tool.invoke({})
    assert out.startswith("[error]")
    assert "path" in out.lower()


async def test_read_file_directory(workspace: Path) -> None:
    """读目录应失败"""
    tool = ReadFileTool(workspace=workspace)
    out = await tool.invoke({"path": "sub"})
    assert out.startswith("[error]")
    assert "regular file" in out.lower() or "not a regular" in out.lower()
