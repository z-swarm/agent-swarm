"""单元测试：Swarm.from_yaml + 配置解析"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agent_swarm.core.swarm import Swarm


def _write_yaml(path: Path, cfg: dict) -> Path:
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return path


def test_from_yaml_minimal(tmp_path: Path) -> None:
    cfg = {
        "name": "demo",
        "agents": [
            {
                "id": "a1",
                "role": "reader",
                "persona": "be brief",
                "provider": "openai",
                "model": "gpt-4o-mini",
                "tools": ["read_file"],
            }
        ],
        "tasks": [{"title": "hello", "description": "say hi"}],
    }
    p = _write_yaml(tmp_path / "s.yaml", cfg)
    swarm = Swarm.from_yaml(p)

    assert swarm.name == "demo"
    assert len(swarm.agents) == 1
    assert swarm.agents[0].id == "a1"
    assert swarm.agents[0].capabilities.allowed_tools == {"read_file"}
    assert len(swarm.tasks) == 1
    assert swarm.tasks[0].id == "t-0"
    assert swarm.tasks[0].title == "hello"


def test_from_yaml_missing_agents(tmp_path: Path) -> None:
    p = _write_yaml(tmp_path / "s.yaml", {"name": "x", "tasks": [{"title": "t"}]})
    with pytest.raises(ValueError, match="agents"):
        Swarm.from_yaml(p)


def test_from_yaml_missing_tasks(tmp_path: Path) -> None:
    cfg = {
        "name": "x",
        "agents": [
            {
                "id": "a",
                "role": "r",
                "provider": "openai",
                "model": "gpt-4o-mini",
            }
        ],
    }
    p = _write_yaml(tmp_path / "s.yaml", cfg)
    with pytest.raises(ValueError, match="tasks"):
        Swarm.from_yaml(p)


def test_from_yaml_agent_missing_required(tmp_path: Path) -> None:
    cfg = {
        "name": "x",
        "agents": [{"id": "a"}],  # 缺 role/provider/model
        "tasks": [{"title": "t"}],
    }
    p = _write_yaml(tmp_path / "s.yaml", cfg)
    with pytest.raises(ValueError, match="missing required"):
        Swarm.from_yaml(p)


def test_from_yaml_task_missing_title(tmp_path: Path) -> None:
    cfg = {
        "name": "x",
        "agents": [
            {
                "id": "a",
                "role": "r",
                "provider": "openai",
                "model": "gpt-4o-mini",
            }
        ],
        "tasks": [{"description": "no title here"}],
    }
    p = _write_yaml(tmp_path / "s.yaml", cfg)
    with pytest.raises(ValueError, match="title"):
        Swarm.from_yaml(p)


def test_from_dict_workspace_inferred(tmp_path: Path) -> None:
    """from_yaml 应把 workspace 默认设为 yaml 所在目录"""
    cfg = {
        "name": "demo",
        "agents": [
            {
                "id": "a1",
                "role": "r",
                "provider": "openai",
                "model": "gpt-4o-mini",
            }
        ],
        "tasks": [{"title": "t"}],
    }
    p = _write_yaml(tmp_path / "s.yaml", cfg)
    swarm = Swarm.from_yaml(p)
    assert swarm.workspace == tmp_path.resolve()


def test_from_yaml_description_null_falls_back_to_title(tmp_path: Path) -> None:
    """B3 回归：description: null 不应变成字符串 'None' 注入 LLM"""
    cfg = {
        "name": "x",
        "agents": [
            {
                "id": "a",
                "role": "r",
                "provider": "openai",
                "model": "gpt-4o-mini",
            }
        ],
        "tasks": [{"title": "do something", "description": None}],
    }
    p = _write_yaml(tmp_path / "s.yaml", cfg)
    swarm = Swarm.from_yaml(p)
    # null description 应回退到 title，而非字面字符串 "None"
    assert swarm.tasks[0].description == "do something"
    assert "None" not in swarm.tasks[0].description


def test_from_yaml_description_empty_falls_back(tmp_path: Path) -> None:
    """B3 回归：空字符串 description 同样回退到 title"""
    cfg = {
        "name": "x",
        "agents": [
            {
                "id": "a",
                "role": "r",
                "provider": "openai",
                "model": "gpt-4o-mini",
            }
        ],
        "tasks": [{"title": "the title", "description": ""}],
    }
    p = _write_yaml(tmp_path / "s.yaml", cfg)
    swarm = Swarm.from_yaml(p)
    assert swarm.tasks[0].description == "the title"


def test_from_yaml_max_iterations_invalid_type(tmp_path: Path) -> None:
    """B6 回归：YAML 写错类型应在加载时拒绝，而非 runner 跑时崩"""
    cfg = {
        "name": "x",
        "agents": [
            {
                "id": "a",
                "role": "r",
                "provider": "openai",
                "model": "gpt-4o-mini",
                "max_iterations": "five",
            }
        ],
        "tasks": [{"title": "t"}],
    }
    p = _write_yaml(tmp_path / "s.yaml", cfg)
    with pytest.raises(ValueError, match="max_iterations"):
        Swarm.from_yaml(p)


def test_from_yaml_max_iterations_zero_rejected(tmp_path: Path) -> None:
    """B6 回归：max_iterations=0 在加载阶段拒绝"""
    cfg = {
        "name": "x",
        "agents": [
            {
                "id": "a",
                "role": "r",
                "provider": "openai",
                "model": "gpt-4o-mini",
                "max_iterations": 0,
            }
        ],
        "tasks": [{"title": "t"}],
    }
    p = _write_yaml(tmp_path / "s.yaml", cfg)
    with pytest.raises(ValueError, match=">= 1"):
        Swarm.from_yaml(p)


# ---------------------------------------------------------------------------
# 依赖解析与循环检测（W2-B9）
# ---------------------------------------------------------------------------


def _agent() -> dict:
    return {
        "id": "a",
        "role": "r",
        "provider": "openai",
        "model": "gpt-4o-mini",
    }


def test_depends_on_resolved_by_title(tmp_path: Path) -> None:
    """depends_on 写 title 应被解析为 task id"""
    cfg = {
        "name": "deps",
        "agents": [_agent()],
        "tasks": [
            {"id": "T1", "title": "first"},
            {"id": "T2", "title": "second", "depends_on": ["first"]},
        ],
    }
    p = _write_yaml(tmp_path / "s.yaml", cfg)
    swarm = Swarm.from_yaml(p)
    assert swarm.tasks[1].depends_on == ["T1"]


def test_depends_on_resolved_by_id(tmp_path: Path) -> None:
    cfg = {
        "name": "deps",
        "agents": [_agent()],
        "tasks": [
            {"id": "T1", "title": "first"},
            {"id": "T2", "title": "second", "depends_on": ["T1"]},
        ],
    }
    p = _write_yaml(tmp_path / "s.yaml", cfg)
    swarm = Swarm.from_yaml(p)
    assert swarm.tasks[1].depends_on == ["T1"]


def test_depends_on_unknown_raises(tmp_path: Path) -> None:
    cfg = {
        "name": "deps",
        "agents": [_agent()],
        "tasks": [{"id": "T1", "title": "x", "depends_on": ["ghost"]}],
    }
    p = _write_yaml(tmp_path / "s.yaml", cfg)
    with pytest.raises(ValueError, match="ghost"):
        Swarm.from_yaml(p)


def test_depends_on_ambiguous_title_raises(tmp_path: Path) -> None:
    """两个任务同名 title——depends_on 用 title 引用应报歧义错"""
    cfg = {
        "name": "amb",
        "agents": [_agent()],
        "tasks": [
            {"id": "T1", "title": "dup"},
            {"id": "T2", "title": "dup"},
            {"id": "T3", "title": "user", "depends_on": ["dup"]},
        ],
    }
    p = _write_yaml(tmp_path / "s.yaml", cfg)
    with pytest.raises(ValueError, match="ambiguous"):
        Swarm.from_yaml(p)


def test_depends_on_cycle_detected(tmp_path: Path) -> None:
    """W2-B9 回归：A→B→A 循环依赖应在加载阶段拒绝"""
    cfg = {
        "name": "cycle",
        "agents": [_agent()],
        "tasks": [
            {"id": "A", "title": "a", "depends_on": ["B"]},
            {"id": "B", "title": "b", "depends_on": ["A"]},
        ],
    }
    p = _write_yaml(tmp_path / "s.yaml", cfg)
    with pytest.raises(ValueError, match="cycle"):
        Swarm.from_yaml(p)


def test_depends_on_three_node_cycle(tmp_path: Path) -> None:
    """A→B→C→A 三节点环也应被检出"""
    cfg = {
        "name": "cycle3",
        "agents": [_agent()],
        "tasks": [
            {"id": "A", "title": "a", "depends_on": ["C"]},
            {"id": "B", "title": "b", "depends_on": ["A"]},
            {"id": "C", "title": "c", "depends_on": ["B"]},
        ],
    }
    p = _write_yaml(tmp_path / "s.yaml", cfg)
    with pytest.raises(ValueError, match="cycle"):
        Swarm.from_yaml(p)


def test_depends_on_self_loop_detected(tmp_path: Path) -> None:
    """A 依赖自己——也是环"""
    cfg = {
        "name": "self",
        "agents": [_agent()],
        "tasks": [{"id": "A", "title": "a", "depends_on": ["A"]}],
    }
    p = _write_yaml(tmp_path / "s.yaml", cfg)
    with pytest.raises(ValueError, match="cycle"):
        Swarm.from_yaml(p)


def test_depends_on_dag_passes(tmp_path: Path) -> None:
    """合法 DAG（A→B, A→C, B→D, C→D）应通过"""
    cfg = {
        "name": "dag",
        "agents": [_agent()],
        "tasks": [
            {"id": "A", "title": "a"},
            {"id": "B", "title": "b", "depends_on": ["A"]},
            {"id": "C", "title": "c", "depends_on": ["A"]},
            {"id": "D", "title": "d", "depends_on": ["B", "C"]},
        ],
    }
    p = _write_yaml(tmp_path / "s.yaml", cfg)
    swarm = Swarm.from_yaml(p)
    assert len(swarm.tasks) == 4


def test_assigned_to_unknown_agent_rejected(tmp_path: Path) -> None:
    cfg = {
        "name": "x",
        "agents": [_agent()],
        "tasks": [{"id": "T", "title": "t", "assigned_to": "ghost"}],
    }
    p = _write_yaml(tmp_path / "s.yaml", cfg)
    with pytest.raises(ValueError, match="ghost"):
        Swarm.from_yaml(p)
