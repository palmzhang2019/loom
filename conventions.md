# Loom 约定 · Conventions

> 本文件收录 Loom 项目里**只有人类能定**的命名/锚点约定。
> 第一条:REQ-ID 命名规范(ID 主线的根)。

---

## REQ-ID 命名规范

**格式**:`<模块>-REQ-<编号>`,例如 `AUTH-REQ-001`。

**粒度**:一条 REQ = 一个用户能感知的小能力,一句话讲得清,往下能切成 2–3 个 segment。比"整个功能模块"细,比"单个函数"粗。

**模块前缀**:大写字母缩写,2–5 个字母(如 `AUTH`、`SYNC`)。模块**按需生长**——不预先定义模块表;每当某个真实功能引出一个新模块,再追加它的前缀。

**编号**:**全局唯一、连续递增**,三位数,从 `001` 起。编号不分模块、不归零——`AUTH-REQ-001` 后下一条无论属于哪个模块都接 `002`。编号只保证唯一与先后,不表示"某模块的第几条"。

**稳定性**:REQ-ID 一经分配不复用、不重排;需求作废则该 ID 退休、空号不填。它是 ID 主线的根,要保证可回溯。

---

## segment_id ↔ REQ-ID 对应

**形式**:`segment_id` 采用 `<REQ-ID>/S<n>`,例如 `MAT-REQ-001/S2`。

**含义**:segment_id 内嵌它所属的 REQ-ID,从事件日志(`LOOM-BUILD-BRIEF.md` 4.4 草案字段含 `segment_id`)可直接反查到需求,**无需独立映射表**。这保证 `需求 → segment → 事件` 的 ID 主线天然连通。

> 事件 schema 的字段实现归实现 agent(P0 提议);本约定只锁定 segment_id 与 REQ-ID 的对应形式。

## 双平面拓扑(Two-Plane Topology)

Loom 跨两个 repo 运行,职责严格分离,REQ-ID 是唯一缝合线。

- **控制平面 = `loom` repo**:持有**意图**(conventions / specs / ROADMAP)与**观测**(事件日志)。是观测者,不放被开发的业务代码。
- **执行平面 = `lingua-web` repo**(及未来其他被 Loom 开发的项目):放**实际业务代码**;segment 的 sandbox(git worktree)在此 repo 开,合入也在此 repo。
- **缝合线 = REQ-ID**:两平面之间唯一的对应关系。`loom` 里的意图与事件通过 REQ-ID(及 `segment_id = <REQ-ID>/S<n>`)指向 `lingua-web` 里的代码改动。除 REQ-ID 外,两平面不共享其他标识。

实现要点(给 P3 的 agent):
- worktree / sandbox 在**执行平面 repo**(如 `lingua-web`)创建与销毁,**不在** `loom`。
- segment 完成后,代码合入执行平面 repo;交接记录(意图蒸馏)写回**控制平面** `loom`。

## 事件日志落位(Events Location)

- `events.jsonl`(及运行期产物)落在**控制平面 `loom`**——它是观测者,事件是 Loom 框架的运行产物。
- 执行平面 `lingua-web` 保持干净,只含业务代码,**不**写入 Loom 的事件日志。
- 不变量 B/C 提醒:事件是**事实**,由 harness 观测产生,**不进 git**(见 `loom/.gitignore`)。

## 维护备注

- `tech.md` 与 `ROADMAP.md` 中 `MAT-REQ-001` 的 S1/S2/S3 切分是**同一套**。改动其一须同步另一处。两者均处于"参考非定稿"状态,segment 契约字段留待进入 P2 后再定稿。
