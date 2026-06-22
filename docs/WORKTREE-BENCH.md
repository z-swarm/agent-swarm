# P4-W23 Worktree 压测报告

生成时间: 2026-06-23 07:03:08

| Mode | Ops | Concurrency | Duration(s) | QPS | acquire p50 | acquire p99 | release p50 | release p99 |
|---|---|---|---|---|---|---|---|---|
| unique_keys | 10 | 5 | 0.184 | 108.8 | 43.633 | 55.697 | 46.247 | 56.891 |
| same_key | 10 | 5 | 0.056 | 356.6 | 11.083 | 17.505 | 11.82 | 18.284 |