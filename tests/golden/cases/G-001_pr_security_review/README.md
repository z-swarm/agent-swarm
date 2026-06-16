# Golden Case G-001: PR 安全审查

## 验证目标

agent-swarm 跑一个安全审查 swarm，对 `auth.py` 输出至少 3 类安全问题：
- SQL 注入
- 命令注入
- 硬编码凭证

且不应误报 `safe_query`（参数化查询）或 XSS。

## 运行（mock LLM）

```bash
pytest tests/e2e/test_w4_golden_g001.py -v
```

## 运行（真实 LLM——nightly）

```bash
export OPENAI_API_KEY=sk-...
pytest tests/golden/ -m "phase==1" --llm-real
```

## 文件清单

| 文件 | 作用 |
|------|-----|
| `expected.yaml` | 验收契约（must_find / must_not_claim / 性能上限） |
| `input.yaml` | swarm 配置（含 code-review:security 技能） |
| `auth.py` | 输入物料——含 3 类故意植入的安全问题 |
