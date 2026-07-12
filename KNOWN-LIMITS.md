# Loom · 已知限制 / 延期项

## events writer 并发
- 当前 writer 仅"追加打开 + 单次写完整行",无并发串行化保证(无文件锁)。
- 单进程顺序写正确;多进程/多线程并发写可能交错。
- **必须在 P5(多 segment 并行)之前解决。** 现在不修是刻意的(避免过早优化)。

## events 检索 / 切分
- 当前所有事件写在单个 events.jsonl,定位某次执行需按 run_id/segment_id 过滤。
- 多 run 并行时检索会变麻烦,取证易误读到旧行(P0c 取证时已遇到一次)。
- P3/P5 多 run 并行前需考虑按 run/segment 切分或建检索手段。现在不做(避免过早优化)。

## 操作提醒:grep 源码跳过缓存
- __pycache__ 里的 .pyc 会保留旧模块名,grep 验证改名/删除时会被这些二进制缓存干扰。
- 验证源码时用 grep --include='*.py' 或先清 __pycache__,避免被陈旧缓存误导。

## 命名规约:产物不带 phase 编号
- phase(P3a 等)是建造过程标记,不是产物功能名。带 phase 的命名会渗进文件名/类名/字符串/注释,清理如拔草根(p3a 追了三轮)。
- 让 agent 按功能命名,不按 phase;验收时发现 phase 编号立即拦。

## 契约 scope_paths 必须对齐执行平面真实结构
- P3d-1 首跑真 codex 暴露:契约 scope_paths 写了 src/backend/(真空假设),lingua-web 实际是 app/。
- Codex 自作主张改到等价路径 app/,被 harness 文件观测如实抓成 out_of_scope(anti-simulation 生效)。
- 教训:写 segment 契约的 scope_paths / 路径假设前,须核对执行平面真实目录结构。

## P3d-1 认知:succeeded ≠ 功能正确
- work_result "succeeded" 仅表示 exit 0 且改动在 scope 内(harness 观测),不表示代码功能正确。
- P3d-1 阶段 test 仍是 mock;功能正确性无机制保证,勿把 succeeded 当"代码可用"。
- worktree 跑完即销毁,agent 写的代码不保留——保留/测试/合入是 P3d-2/P3d-3/P4 的事。

## sandbox 环境准备(P3d-2 发现)
- git worktree 只复制代码不带 .venv;fresh worktree 直接跑 pytest 会因缺依赖 collection error(exit 2)。
- 修法:sandbox 创建后经 harness 观测执行 uv sync --extra dev(pytest 在 optional-dependencies.dev,裸 uv sync 不装 extras)。再进 work/test。
- 成本:每次开 sandbox 都 sync 一遍(uv 有缓存,通常可接受);嫌慢再优化,现在不做。

## run_id 唯一性(P3d-3 收尾确立)
- run_id 在同一 events log 中一次性使用;复用会使按 run_id 派生的视图混合多次执行(P3d-3 真实演示曾因此误判"双开 sandbox")。
- 现由确定性闸强制:启动前检查 events log,重复 run_id 直接拒绝。
