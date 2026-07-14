# 交接记录 · Schema (P2)

> 本文件定义 Loom 中 **交接记录 (Handoff Record)** 的字段结构。
> segment 完成时(Stage 4 收口)产出,进 git@main,供下游 segment 消费(落实不变量 F:
> 过程只以"蒸馏成耐久产出"的形式向前流动,原始执行 trace 绝不喂进下游上下文)。
>
> 配套 `segment-contract.schema.md`。两者对口 —— 上游吐的 = 下游要的:
>   上游契约 anti_scope.defer  → 下游交接记录 deferred(origin: contract)
>   上游交接记录 seams          → 下游契约 depends_on 之后要对接的接口
>   上游契约 covers_req         → 交接记录 covers_req

---

## 核心原则:按"错误的代价"分配可信度

同一份交接记录里,信息分两档,依据是**"谁消费、错了会怎样"**:

| 档位 | 字段 | 来源 | 错误后果 |
|---|---|---|---|
| **硬事实** | `seams` / `covers_req` / `pointers` | **harness 确定性抽取**(禁 LLM 自由写) | 下游代码机械依赖,写错→崩,必须准 |
| **软信息** | `deferred.discovered` / `key_decisions` | **LLM 蒸馏** | 人类合入时扫一眼的提示,写漏→人类补判断,不致命 |
| **机械搬运** | `deferred.contract` / `covers_req` | 从上游契约自动流入 | 无判断,直接搬 |

这落实不变量 C/E/F:确定性的归 harness,LLM 只做软信息;事实归抽取,理由归蒸馏。

---

## 字段总览

| 字段 | 来源 | 作用 |
|---|---|---|
| `covers_req` | 抄契约(机械) | 满足了哪个 REQ |
| `seams` | **harness 抽(硬)** | 创建/改动的公共接口 = 下游对接点 |
| `deferred` | 契约带入 + LLM 蒸馏 | 延期项 / 已知限制 |
| `key_decisions` | LLM 蒸馏(软) | 约束未来的决策 + 理由 |
| `pointers` | harness 抽(硬) | 合入 commit / 测试文件 / as-built 图 |
| `merge_status` | harness 搬运合入闸结果 | pending / merged / rejected 生命周期 |
| `reject_reason` | 人类合入闸输入(仅 rejected) | 保留人类给出的拒绝理由 |

---

## 字段定义

### covers_req `(来源:抄契约,机械)`
满足的 REQ-ID,**单个**(与契约 covers_req 一对一一致)。直接取契约值,不重写。

### seams `(来源:harness 从 diff 确定性抽取,禁止 LLM 自由写)`
本 segment 对外暴露的、别的代码要靠它对接的**公共接口点**(接缝)。
- 只记**跨 segment 边界被调用/依赖**的东西;内部实现(私有函数、局部变量)不是 seam。
- **必须由 harness 从真实 diff 抽取**——它是下游 `depends_on` 之后要机械对接的"对接说明书",写错则下游崩。因此不容许 LLM 凭记忆写。
- 埋给 P4/P5 的实现任务:harness 需能从 diff 解析新增/改动的函数签名、路由、DB 变更。
- 抽取采用保守口径:只记录 diff 中结构可见的新增/改动接口(路由、模块顶层函数签名、表结构变更);函数体内未形成顶层接口的逻辑不计 seam,也不从 DML 推断 DB seam。硬事实宁漏勿误报,不作模糊推断。

```yaml
seams:
  - kind: route
    signature: "DELETE /material/{mid}/source-tag/{tid}"
  - kind: function
    signature: "remove_material_tag(material_id, tag_id) -> bool"
# kind ∈ route | function | db | class
```

### deferred `(来源:两类)`
延期项 / 已知限制。两个来源,用 `origin` 区分:
- `origin: contract` —— **从上游契约 anti_scope.defer 自动带入**(计划内延期,机械搬运)。带 `defer_to`。
- `origin: discovered` —— **执行中新发现的限制**,由 LLM 从事件日志/diff 蒸馏(软信息,给人看,容忍模糊)。

```yaml
deferred:
  - text: 前端移除入口与确认弹窗
    origin: contract
    defer_to: MAT-REQ-001/S2
  - text: 未处理并发删除同一关联的竞态
    origin: discovered
```

### key_decisions `(来源:LLM 蒸馏,软信息)`
本 segment 做出的**约束未来**的决策 + 理由(ADR-lite)。
- 来源只能是 LLM 蒸馏(理由不在 diff 里,无法确定性抽取)。
- **只记"约束未来/影响下游"的**:判据是"下游会不会因不知道它而踩坑、或错误推翻一个其实有理由的设计"。平凡选择不记,避免流水账淹没关键条目。

```yaml
key_decisions:
  - decision: 硬删除关联记录,不做软删除
    rationale: 需求明确不做撤销/恢复,软删除会引入无人消费的状态
```

### pointers `(来源:harness 抽,硬)`
指针,全部确定性可得:
```yaml
pointers:
  merge_commit: <合入的 commit hash>
  test_files: [<新增测试文件路径>, ...]
  as_built_diagram: <反向生成的图路径>
```

---

## 完整示例 · MAT-REQ-001/S1 交接记录

```yaml
covers_req: MAT-REQ-001
merge_status: merged
seams:
  - kind: route
    signature: "DELETE /material/{mid}/source-tag/{tid}"
  - kind: function
    signature: "remove_material_tag(material_id, tag_id) -> bool"
deferred:
  - text: 前端移除入口与确认弹窗
    origin: contract
    defer_to: MAT-REQ-001/S2
  - text: 删除后页面重定向与反馈
    origin: contract
    defer_to: MAT-REQ-001/S3
  - text: 未处理并发删除同一关联的竞态
    origin: discovered
key_decisions:
  - decision: 硬删除关联记录,不做软删除
    rationale: 需求明确不做撤销/恢复,软删除会引入无人消费的状态
pointers:
  merge_commit: <hash>
  test_files: ["tests/backend/test_remove_material_tag.py"]
  as_built_diagram: "as-built/MAT-REQ-001-S1.seq.txt"
```

---

## 两 schema 咬合验证(P2 闭合点)

```
上游契约 anti_scope.defer   ──→  下游交接记录 deferred(origin: contract)
上游交接记录 seams          ──→  下游契约 depends_on 之后要对接的接口
上游契约 covers_req         ──→  交接记录 covers_req
```
上游吐的 = 下游要的,字段天然对齐,不用人肉搬运。

## 状态
- 本 schema 于 P2 定稿,与 `segment-contract.schema.md` 配套闭合。
- seams / pointers 的确定性抽取能力属 P4/P5 实现任务;此处只定字段与来源原则。
- deferred.discovered / key_decisions 的 LLM 蒸馏属 Stage 4 实现;此处只定字段与"软信息"定位。

### merge_status `(必填)`
交接记录在产物就绪时即生成(seams/decisions 可在合入前从 branch 抽取),合入结果通过本字段与
`pointers.merge_commit` 反映。取值:

- `pending` —— 交接记录主体已生成(seams、decisions 等),尚未经过人类合入闸。
- `merged` —— 人类合入闸 approve,已 merge 进目标分支;此时 `pointers.merge_commit` 必填。
- `rejected` —— 人类合入闸 reject;附 `reject_reason`(人类给出的拒绝理由)。
  **rejected 状态下不抽取 seams**(被拒产物代码未入库,其接口下游不可依赖);
  交接记录只保留:covers_req、merge_status=rejected、reject_reason、以及已知的 deferred(遗留)。

设计依据:seams 是 harness 从 diff 抽取的事实,合入前就已存在,不必等合入才生成;真正只有合入后才能填的是
`pointers.merge_commit`。故交接记录主体在产物就绪时生成,合入闸的结果只更新 merge_status 与 merge_commit。
这也让被拒的 segment 有一份"为何被拒、留下什么坑"的诊断记录(供复盘、改契约、重跑)。

```yaml
# 合入成功:
merge_status: merged
pointers:
  merge_commit: <hash>
  ...

# 被拒:
merge_status: rejected
reject_reason: "移除功能没有测试覆盖;工作自我报告不可靠"
# 不含 seams;保留 covers_req、deferred
```

### seams 抽取时机(修订)
seams 在【产物就绪、merge_status ∈ {pending, merged}】时由 harness 从 branch 相对 main 的 diff 抽取。
merge_status == rejected 时不抽 seams(见上)。
