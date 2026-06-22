# P4-W23 Worktree 压测报告

生成时间: 2026-06-22 09:52:17

| Mode | Ops | Concurrency | Duration(s) | QPS | acquire p50 | acquire p99 | release p50 | release p99 |
|---|---|---|---|---|---|---|---|---|
| unique_keys | 10 | 5 | 4.521 | 4.4 | 1112.456 | 1484.303 | 1156.14 | 1498.878 |
| same_key | 10 | 5 | 1.108 | 18.0 | 352.556 | 364.814 | 352.741 | 365.405 |