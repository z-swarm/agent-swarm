# P4-W23 Worktree 压测报告

生成时间: 2026-06-22 10:16:07

| Mode | Ops | Concurrency | Duration(s) | QPS | acquire p50 | acquire p99 | release p50 | release p99 |
|---|---|---|---|---|---|---|---|---|
| unique_keys | 10 | 5 | 4.497 | 4.4 | 1065.845 | 1464.039 | 1170.769 | 1491.466 |
| same_key | 10 | 5 | 1.061 | 18.9 | 343.648 | 349.254 | 343.711 | 349.515 |