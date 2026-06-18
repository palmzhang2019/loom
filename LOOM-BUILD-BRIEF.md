# Loom · 构建说明书 (Build Brief)

> 本文档是一份**自包含**的构建指南。你(执行本文档的 AI)可能没有任何先验上下文。
> 请先完整读完「0. 给执行方的工作约定」和「1. 意图与哲学」,再开始任何动作。
> 本文档描述**意图与不变量**,不是逐行实现规格。实现细节由你提议、由人类拍板。

---

## 0. 给执行方的工作约定 (先读这条)

Loom 的作者用严格的过程纪律工作。构建 Loom 本身也必须遵守同样的纪律,否则就违背了 Loom 的精神。

1. **阶段闸 (phase gate)**:严格按 P0→P6 顺序。一个 phase 没有「被人类亲手用过」之前,不得开始下一个。完成的标准不是「代码写完」,而是「作者本人用过并认可」。
2. **不要过度设计**:只构建当前 phase 必需的东西。任何「以后可能有用」的功能一律推迟到 P6 或更后。看到自己在加「为了扩展性」的抽象时,停下来问人类。
3. **单问题推进**:需要澄清时,一次只问一个问题,等回答再继续。不要一口气抛十个问题。
4. **明确承认越界**:如果你发现自己做了超出当前 phase / 当前任务范围的事,主动指出来,不要掩盖。
5. **你是执行者,不是决策者**:设计判断、安全权衡、接口形状的最终决定权在人类。你负责提议和实现。
6. **不要自报状态当事实**:你声称「测试通过了」不算数,要有 harness 观测到的证据(见不变量 C)。

---

## 1. 意图与哲学

Loom 是一个**半自动化的编排 + 执行框架**,用于软件开发。核心分工:

- **人类把关**:前期设计、产品形态、产品审核、最终合入。
- **Agent 执行**:代码编辑、代码 review、测试、审计。

一句话定位:**人类决定「做什么」和「能不能合入」,Agent 负责「怎么做」。**

名字 **Loom(织机)** 来自框架的灵魂——一条贯穿全局的**可追溯主线**:一根 ID 线把 `需求 → slice → 代码 → 测试 → review` 串起来。织机把绷直的经线(ID 主线)固定,让每个开发单元(segment)作为纬线横向织上,最终成布(产品),且布上任意一点都能回溯到某根线。

---

## 2. 核心不变量 (Invariants · 不可违反)

这些是 Loom 的地基。任何实现都不得违反。它们也是判断「设计对不对」的标尺。

- **A. ID 可追溯主线**:每条需求有稳定 ID(如 `REQ-001`)。ROADMAP、mermaid 图节点、segment、代码、测试都引用这些 ID。没有这根线,下游一切对齐都无从谈起。
- **B. 意图 vs 事实,永不混合**:
  - *意图*(计划、决策理由)是相对静态的,进 git。
  - *事实*(进度、执行结果)是派生的,由 harness/事件记录,**绝不手写进文档**。
  - 「现在有哪些接口」永远从代码现读;「为什么这么设计」才读历史记录。
- **C. 执行事实由 harness 观测,不由 agent 自报**:数据库/日志记录的是 harness 抓到的真相(改了哪些文件、跑了哪些测试、exit code),不是 agent 说它做了什么。这是 anti-simulation 原则。
- **D. 两条独立边界**:
  - *sandbox 边界 = 谁共享文件*(隔离需求)→ **per segment**。
  - *session 边界 = 谁共享上下文/思考*(独立性需求)→ 按角色切分。
  - 不要用一个边界套两件事。
- **E. 阻断性硬闸必须是确定性代码,不是 LLM 判断**。LLM 只做补充性的「软告警 + 升级人类」,绝不让最后的安全闸依赖 LLM。
- **F. 过程只能以「蒸馏成耐久产出」的形式向前流动**。原始执行 trace 绝不喂进下一个 segment 的上下文;它只流向「审计」和「人类调试/系统改进」。
- **G. 人类合入闸不可省略**:agent 的 review+audit 全绿也不自动合入。人类看**报告和反向生成的图**(不逐行看代码)后点击合入。这是生产力收益与安全的平衡点。

---

## 3. 架构概览

四个阶段。Stage 1 是人类的,Stage 2-4 逐步交给 agent,但 stage 之间和合入处有人类闸。

```
Stage 1  Plan (人类)          → 产品方案 + 技术方案 + ROADMAP,带 REQ-ID
Stage 2  Slice generate       → 从 ROADMAP 切出自包含的 segment 契约 (陪跑确认)
Stage 3  Orchestrate+Work+Test→ 在 sandbox 内执行 segment (LangGraph 编排)
Stage 4  Review + Audit + 合入 → 对比契约 / 安全闸 / 人类点合入
```

### 3.1 技术选型

- **编排层:LangGraph**。选它因为它是显式有向图 + 条件边 + 内置 checkpointing + 一流的 human-in-the-loop interrupt,契合 Loom「图状控制流 + 人类审批闸」的本质。
  - orchestrator / work / test = LangGraph 节点。
  - `implement → test → 失败则 fix` 的循环 = 一条条件边(cycle)。**第一版固定这条边,不要动态拆分**(那是 P6)。
  - 陪跑确认(P2)和人类合入闸(P4)= LangGraph human interrupt。
  - **重要坑**:LangGraph 的 checkpointer 是给图恢复/续跑用的,**不要拿它当进度真相源**(那等于 agent 自报状态,违反不变量 C)。进度永远从事件日志派生;checkpoint 只管内部恢复。两者分开。
- **事件存储:起步用 append-only JSONL**,不要一上来上数据库(不变量:不过度设计)。查询需求变复杂再升级到 SQLite。
- **Sandbox:git worktree / branch**(或容器),per segment 创建,合入后销毁。
- **work agent / test agent**:可在 LangGraph 节点里接现成的 coding agent(如 Claude Agent SDK 或带 sandbox 执行的方案)。MCP 工具(如已有的 permission server)可原生接入。

### 3.2 session 与 sandbox 的对应 (落实不变量 D)

- 1 个 segment = 1 个 sandbox(贯穿该 segment 内所有 step,因为 step 间要在彼此文件上继续干)。
- 1 个 segment = 1 个 orchestrator session(大脑,持有 segment 契约,决定 step)。
- N 个 work session(orchestrator 派生,执行 step;早期可共享上下文)。
- **test session 必须 fresh**,只喂 spec/验收标准/sequence diagram,**绝不看 work 的实现上下文**(否则测试退化成「迎合实现」,利益冲突)。
- review / audit 各自再开 fresh session,喂 artifact(diff、反向生成的图),不喂 work 的思考过程。

---

## 4. 关键工件 (Artifacts)

### 4.1 Plan 三件套 (Stage 1, 人类产出, 进 git@main)
- `product.md` — what / why,相对稳定,人类把关。
- `tech.md` — how,易变,agent 主要消费。
- `ROADMAP.md` — 开发大节点,引用 REQ-ID。**只是规划意图,不记实时进度**(进度是派生的)。

### 4.2 Segment 契约 (Stage 2 产出)
必须**自包含**到:Stage 3 的执行 agent 不需要再读全局大 plan 就能干活。至少包含:
- 覆盖哪些 `REQ-ID`
- 验收标准 (acceptance criteria,可验证)
- sequence diagram + html 预览(**既是预览也是验收靶子**,实现必须对齐它)
- 对前序 segment 的依赖(此列表 = 下游该加载哪些交接记录的选择器)
- **anti-scope**:明确说这个 segment **不做**什么(Stage 4 防 scope 蔓延的判据)
- 切分原则:**按「可评审性」切,不按「功能完整性」切**——人类能在一次专注里读完契约、产出的 diff 可理解。

### 4.3 交接记录 (Handoff Record, Stage 4 收口时产出, 进 git@main)
segment 完成时对「执行过程」的**蒸馏**,供下游消费(落实不变量 F)。最小字段:
- 满足了哪些 `REQ-ID`
- **创建/改动的公共接口(接缝)**:新 class、函数、API、DB 表/字段 —— 这是下游真正对接的东西。**能确定性抽取的(接缝、commit、测试文件)由 harness 从 diff/git 抽,不让 LLM 自由写**。
- **关键决策 + 理由**(约束未来的那种,ADR-lite)—— 这部分由 LLM 从事件日志蒸馏。
- 已知限制 / 延期项(复用 anti-scope)。
- 指针:合入 commit、新增测试文件、as-built 图。

### 4.4 事件日志 (events, harness 产出, 不进 git)
```
每行一个事件:{ts, segment_id, run_id, actor, type, payload}
actor: orchestrator / work / test / review / audit / harness
type:  step_started / file_changed / test_run / gate_blocked / ...
```
用途:可观测性视图 + 派生进度 + 审计轨迹(一物三用)。**进度是对它的 query,不是存储的字段。**

---

## 5. Stage 4 安全闸清单 (audit)

区分:**review 问「对不对/好不好」(可建议);audit 问「安不安全/有没有越界」(应阻断)**。下面多数是确定性扫描(硬闸),LLM 只补软告警。

1. **密钥/机密**(最高优先):代码和日志有无明文 secret。应卡在**日志写入层**,而非事后扫描。
2. **供应链**:新增依赖的许可证 / 已知 CVE / 维护状态(接住「agent 自由选框架」的风险)。
3. **越界**:有无越出 sandbox、不该有的网络出口、动 segment scope 外的文件。
4. **破坏性操作**:DB migration / 删除 / schema 变更 —— 即使在「agent 自由」区也强制人类闸。
5. **数据/隐私**:PII 处理、敏感数据落日志。
6. **资源/成本**:死循环、API 爆量、token 预算上限。
7. **证据完整性**:agent 声称跑过的测试,harness 是否真的看到(anti-simulation)。

review agent 至少查:① spec 一致性(反向生成图对比契约)② scope 遵守(anti-scope)③ 测试充分性(是否恒真废测试)④ 跨 segment 架构一致性(ERD/class 图)⑤ 基础代码质量。

---

## 6. 构建顺序 (P0 → P6)

每个 phase 以**「作者亲手用过并认可」**为完成标志。phase 间是硬闸,子任务顺序是建议序。

- **P0 · 事件底座**
  - P0a 定义 event schema(ts/segment_id/run_id/actor/type/payload)
  - P0b append-only JSONL writer
  - P0c harness 观测包装(抓 file_changed / test_run / exit_code)
  - P0d 极简只读视图(tail / 静态页)— **必须在 P3 之前可用**,否则 P3 是黑盒
  - 闸:手写事件能在视图看到

- **P1 · Plan 契约 + ID 主线**
  - P1a REQ-ID 命名规范
  - P1b product.md 模板  P1c tech.md 模板  P1d ROADMAP.md 引用 REQ-ID
  - 闸:给一个真实小功能写出 plan

- **P2 · Segment 契约 + 交接记录 schema**
  - P2a 契约字段(见 4.2)  P2b seq 图 + html 预览  P2c 交接记录 schema(见 4.3)  P2d slice-generate 陪跑(先半手动)
  - 闸:产出一个自包含 segment 契约

- **P3 · 单 segment 执行 (LangGraph 上场)**
  - P3a LangGraph 骨架(orchestrator/work/test 节点)  P3b sandbox=git worktree 创建/销毁  P3c work/test session 分离  P3d implement→test→fix 条件边  P3e 全程写 events
  - 闸:看一个 segment 在视图里跑绿

- **P4 · review + audit + 人类合入闸**
  - P4a 反向生成图/html 对比契约  P4b 确定性硬闸  P4c LLM 软告警  P4d human interrupt 合入闸  P4e merge→生成交接记录
  - 闸:跑通一个完整 segment,读报告点合入

- **P5 · 多 segment 串联**
  - P5a 干净上下文起步  P5b 按契约依赖加载交接记录  P5c 接口现读(diff/AST)  P5d 串跑两个有依赖的 segment
  - 闸:跑通两个有依赖的 segment

- **P6 · 增强 (暂缓, 别提前做)**
  - 动态 step 拆分 / 回归套件累积 / 漂亮 UI / 并行 segment

---

## 7. 以后怎么改进 (调优层, 由浅到深)

出问题时,**用最浅的层去修**,别动辄改 prompt:

1. **Segment 契约** — ~80% 的改动在这。产出不对,多半是 spec 有歧义。
2. **Prompt(agent 角色定义)** — 仅当某 agent 跨多个 segment **系统性**犯同类错时才动。
3. **Gate/audit 规则(确定性代码)** — 某类风险漏过,或误报挡太狠。
4. **Harness/orchestration 逻辑** — step 拆分或观测本身错了。
5. **stage 间 schema/契约** — 最深,极少动,一动全下游受影响。

---

## 8. 回归测试的位置

回归不是 per-segment「生成」的,它是**跨 segment 的累积套件执行**——「所有历史 segment 的机能测试现在还得全绿」。segment 契约里定义的是单测 + 本 segment 机能测试;回归是把它们汇入总套件(P6 才正式做累积机制)。

---

*本文档描述意图与不变量。遇到本文档未覆盖的实现选择,提议方案并问人类,不要自行假设。*
