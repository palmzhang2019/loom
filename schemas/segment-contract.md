# Segment 契约 · Schema (P2)

> 本文件定义 Loom 中 **segment 契约** 的字段结构。契约在 Stage 2 (slice generate) 产出,
> 必须**自包含**到:Stage 3 的执行 agent 不需再读全局大 plan 就能干活。
> 本 schema 的每个字段都以试点功能 `MAT-REQ-001`(素材详情页支持移除来源标签)的
> 真实 S1/S2/S3 校验后定稿,非真空设计。
>
> 关联:本 schema 与 `handoff-record.schema.md`(交接记录)配套 —— 上游契约的
> `anti_scope.defer` 项流入下游可读的延期清单,`depends_on` 决定加载哪些上游交接记录。
> 两者须对口(上游吐的 = 下游要的)。

---

## 字段总览

| 字段 | 必填 | 形态 | 作用 |
|---|---|---|---|
| `segment_id` | 是 | `<REQ-ID>/S<n>` | 唯一标识,ID 主线锚点 |
| `covers_req` | 是 | 单个 REQ-ID | 本 segment 服务的需求(一对一) |
| `title` | 是 | 一句话 | 人类可读的 segment 名 |
| `acceptance` | 是 | 带编号的断言列表 | 验收标准 = test 派生源 + review 靶子 |
| `anti_scope` | 是 | 分类列表 | 明确不做什么,P4 越界/抢跑判据 |
| `depends_on` | 是 | segment_id 列表 | 前序依赖 = 交接记录加载选择器 + 执行序 |
| `scope_paths` | 是 | 目录前缀列表 | 允许触碰的文件范围,P4 越界判断锚 |
| `preview` | 是* | seq 图 + (条件)html | 预览 + 验收靶子(漂移检测) |

---

## 字段定义

### segment_id `(必填)`
格式 `<REQ-ID>/S<n>`,如 `MAT-REQ-001/S1`。内嵌 REQ-ID,从事件日志可直接反查需求,无需独立映射表(见 `conventions.md`)。

### covers_req `(必填)`
**单个** REQ-ID,一对一。一个 segment 只服务一个 REQ。
- 若一个改动天然横跨两个 REQ,是信号:拆成两个 segment,或反思两个 REQ 是否该合并。
- 复杂度顶回到"REQ 该怎么切"那一层解决,不松动本约束(用最浅的层修)。

### title `(必填)`
一句话描述,人类可读。例:`后端移除来源标签的路由与删除逻辑`。

### acceptance `(必填)`
可验证的验收标准列表,每条**带可追溯编号** `<segment_id>/AC<n>`。
- 每条必须能翻译成一个通过/不通过的测试(不能是"体验流畅"这类含糊话)。
- 是 test agent 派生测试的来源、review agent 判断"做对没有"的靶子。

```yaml
acceptance:
  - id: MAT-REQ-001/S1/AC1
    text: 存在一个后端路由,接收"移除某个来源标签关联"的请求
  - id: MAT-REQ-001/S1/AC2
    text: 删除的是【标签与素材的关联记录】,不是标签本身
  - id: MAT-REQ-001/S1/AC3
    text: 删除成功后,该素材的来源标签列表中不再包含被移除项
  - id: MAT-REQ-001/S1/AC4
    text: 对不存在的关联发起删除,返回明确失败/无操作,不报 500
```

### anti_scope `(必填)`
明确本 segment **不做**什么。每项分两类(`kind`):
- `defer` —— **会**做,只是不在本 segment。带 `defer_to` 指向目标 segment。
  → 流入下游交接记录的"延期清单";P4 命中 = **抢跑(警告)**。
- `out_of_req` —— 整个 REQ 范围内都**不做**。
  → 不流入下游;P4 命中 = **真越界(阻断)**。

```yaml
anti_scope:
  - text: 前端移除入口与确认弹窗
    kind: defer
    defer_to: MAT-REQ-001/S2
  - text: 删除后页面重定向与反馈
    kind: defer
    defer_to: MAT-REQ-001/S3
  - text: 批量移除
    kind: out_of_req
  - text: 标签本身的增删(只删关联)
    kind: out_of_req
```

### depends_on `(必填)`
前序 segment 依赖,**segment 级**(记到整个 segment,不细到接口)。空列表 = 无前序。
- 是**选择器**:P5 时下游据此加载哪几份上游交接记录(不全读,只读依赖的)。
- 隐含**执行序**:依赖关系构成有向图,即 segment 执行次序,无需另设顺序字段。

```yaml
# S1: depends_on: []
# S2: depends_on: [MAT-REQ-001/S1]
# S3: depends_on: [MAT-REQ-001/S1, MAT-REQ-001/S2]
```

### scope_paths `(必填)`
本 segment 允许触碰的文件范围,**目录前缀**(非精确文件)。
- harness 观测到的改动文件落在前缀下 → 正常;跑出前缀 → P4 判**越界**(确定性路径比对,合不变量 E)。
- 目录前缀给 agent 目录内的合理自由;"同目录内动了不该动的文件"目录级放过,需要时再收紧到精确文件。

```yaml
scope_paths:
  - src/backend/routes/
  - src/backend/models/
```

### test_selectors `(必填,允许空列表)`
本 segment 该跑哪些现成测试(P3d-2/B2:跑 scope 相关的测试子集,由人指定,不让机器猜)。

- **粒度:文件级**。列出 pytest 可用的测试文件路径(相对执行平面 repo 根),不做函数级(`::` 精确)——够用即可,函数级等真需要再加。
- **必填但允许空列表**:字段必须存在(schema 完整性)。若本 segment 无合适的现成测试可跑,填 `[]`——这是一条有意义的信息("无现成测试"),不填不相关测试凑数(凑数是自欺,违背 anti-simulation)。
  - 空列表时:test 节点诚实跳过,test_result 标记 `skipped`;系统不自动补 py_compile。"该 segment 无测试是否可接受"交由 P4 review 判断(review 本就查测试充分性)。
- **与 scope_paths 的约束**:test_selectors 指向的测试文件**不得出现在 scope_paths 中**。scope_paths 只含实现文件(如 `app/`),测试文件(`tests/`)在其外——于是现有 scope 检查天然拦住 agent 改测试:agent 只能改实现(scope 内)让测试通过,不能改测试本身让它迎合实现(利益冲突,P3c 隔离的延续)。
- **来源**:人在写契约时指定(与 acceptance 同属人的判断)。test 节点从契约读取,在 sandbox 内经 harness 观测执行 `uv run pytest <test_selectors>`,pass/fail 由观测的 exit_code 决定。

```yaml
test_selectors:
  - tests/test_s3t_tagging.py
  - tests/test_s4bb_material_tag_wiring.py
# 无现成测试时:test_selectors: []
```

### preview `(seq 图必填;html 条件必填)`
**既是预览,也是验收靶子** —— P4 从代码反向生成图/html,与此比对,检测漂移。

- `sequence_diagram` —— **必填,且必须是合法 mermaid `sequenceDiagram`**(含 participant 声明与箭头语法),
  不接受伪代码或自由文本。任何 segment 都有交互时序(后端也是 `请求→路由→删关联→返回`),
  它是 test agent 的黑盒 oracle(照时序验,不看实现)。
  - 理由:它既是设计意图的表达,也是 review 反向生成对比的基准 —— 两边同为合法 mermaid 才能对比;
    且能在 review 报告中直接渲染成图供人类审阅,服务于"人类看图与报告做判断,不逐行读代码"。
  - 一致性要求:须完整表达契约的 acceptance,**包括失败/边界路径**(用 alt/else 表达)。
    若某条 AC 描述了一条路径(如"关联不存在时返回明确失败"),时序图须体现它;
    否则反向生成的实现含该分支、契约图没有,对比会误报漂移。

- `html_preview` —— **条件必填**:仅当 segment 有用户可见界面时要求。
  触发条件:`scope_paths` 是否含 UI 目录(如 `src/frontend/`)——含则必填,纯后端免。
  (关联逻辑的实现属 P4,此处仅定义规则。)

```yaml
# S1(后端,无 UI):sequence_diagram 有,html_preview 免
# S2(详情页,有 UI):两者都要
```

---

## 完整示例 · MAT-REQ-001/S1

```yaml
segment_id: MAT-REQ-001/S1
covers_req: MAT-REQ-001
title: 后端移除来源标签的路由与删除逻辑
acceptance:
  - id: MAT-REQ-001/S1/AC1
    text: 存在一个后端路由,接收"移除某个来源标签关联"的请求
  - id: MAT-REQ-001/S1/AC2
    text: 删除的是【标签与素材的关联记录】,不是标签本身
  - id: MAT-REQ-001/S1/AC3
    text: 删除成功后,该素材的来源标签列表中不再包含被移除项
  - id: MAT-REQ-001/S1/AC4
    text: 对不存在的关联发起删除,返回明确失败/无操作,不报 500
anti_scope:
  - text: 前端移除入口与确认弹窗
    kind: defer
    defer_to: MAT-REQ-001/S2
  - text: 删除后页面重定向与反馈
    kind: defer
    defer_to: MAT-REQ-001/S3
  - text: 批量移除
    kind: out_of_req
  - text: 标签本身的增删(只删关联)
    kind: out_of_req
depends_on: []
scope_paths:
  - src/backend/routes/
  - src/backend/models/
preview:
  sequence_diagram: |
    请求 -> 路由: 移除关联(material_id, tag_id)
    路由 -> 模型: 删除关联记录
    模型 --> 路由: 删除结果
    路由 --> 请求: 成功/失败响应
  # html_preview: 免(S1 无 UI)
```

---

## 状态

- 本 schema 于 P2 定稿,7 个字段全部以 MAT-REQ-001 校验。
- 配套的 `handoff-record.schema.md`(交接记录)**待定**——两者须对口收尾:
  上游 `anti_scope.defer` = 下游延期清单;上游产出的接缝 = 下游 `depends_on` 要对接的接口。
- P4 如何用这些字段判越界/抢跑,属 P4 阶段,此处只留字段,不定判断算法(避免真空设计)。