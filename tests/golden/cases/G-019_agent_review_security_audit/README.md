# G-019 Golden Case

**场景**: agent_review.py --mode=simple 识别本项目 PR diff 中的真实安全问题

**对应**: DESIGN §15 Phase 2 末期 Dogfooding + W15 DoD ⑦

**W15 落地**:
- `tools/agent_review.py` 7 类静态安全规则（secret_leak / cmd_injection / path_traversal / eval / sql_injection / data_exposure / weak_hash）
- W15 新增 `--require-human-review` + `--approve-override` + `--fail-on` flag
- W15 同步前置 G-019 跑通

**验收**:
- 跑 `python tools/agent_review.py --mode=simple` 在含已知问题的 fixture diff 上 → must_find 全中
- 跑 `python tools/agent_review.py --mode=simple --require-human-review` 在有 CRITICAL 时 → exit 2
- 跑 `python tools/agent_review.py --mode=simple --require-human-review --approve-override` → exit 0
