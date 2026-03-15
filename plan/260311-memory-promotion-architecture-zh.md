# Memory Promotion Architecture

最后更新：2026-03-11

## 背景

当前仓库在分层记忆方面已经有了一个不错的起点：

- 它可以从 Claude Code 和 Codex 日志中提取持久候选记忆。
- 它可以保留带证据的每日日志。
- 它可以重建整理后的 `MEMORY.md`。

目前仍然缺少的是一个清晰的中间层。

现在的流水线大致是：

`transcript -> candidate log -> curated MEMORY.md`

这对于长期运行来说还不够。

- 如果太多内容进入 `MEMORY.md`，这个文件会变得嘈杂。
- 如果进入 `MEMORY.md` 的内容太少，后续会话会丢失有用细节。
- 如果没有一个可搜索层，长尾上下文就没有地方可以存放。

解决办法是把三层显式化：

`raw -> searchable -> MEMORY.md`

这份文档定义了 v1 的这套架构。

## 使用场景

目标工作流如下：

1. 每轮对话结束后，一个 hook 监视新增的 transcript 窗口。
2. 系统从这个窗口中提取候选记忆。
3. 所有被接受的候选项都带着证据保存在本地。
4. 稳定事实逐步累积到一个可搜索的本地存储中。
5. 只有最高价值的事实会被提升进 `MEMORY.md`。
6. 在下一次 prompt 之前，系统会注入：
   - 作为常驻记忆的 `MEMORY.md`
   - 在相关时从 searchable 层取出的一个小型 recall 区块

这样可以让 `MEMORY.md` 保持简短和稳定，同时又让系统记住更多内容。

## 方案

### 1. 三层记忆模型

| 层级 | 目的 | 事实来源 | 读取方式 |
|------|------|----------|----------|
| `raw` | 提取观察结果的追加式证据日志 | JSONL | 审计和重处理 |
| `searchable` | 用于召回的聚合工作记忆 | JSONL | 查询时检索 |
| `MEMORY.md` | 小型、始终注入的记忆 | Markdown | 每次会话注入 |

规则：

- `raw` 覆盖面较广，但仍然经过过滤。它存储的是持久候选项，而不是完整 transcript。
- `searchable` 是主要的工作记忆层。
- `MEMORY.md` 是提升后的摘要，不是数据库。

### 2. v1 的范围与默认值

v1 的决定固定如下：

- Scope：仅 `project`
- Capture：`post-turn`
- Recall：`pre-turn`
- Searchable storage：优先 `JSONL`
- Search backend：优先本地词法检索，v1 不依赖向量能力
- v1 不依赖 mem0

这样可以让第一个版本保持小而且容易验证。

### 3. 存储布局

项目本地的 layered 输出应演进为：

```text
output/
  .state/{project_slug}.json
  project/{project_slug}/
    MEMORY.md
    memory/
      raw/
        YYYY-MM-DD.jsonl
      searchable/
        facts.jsonl
        archive/
          YYYY-MM-DD.jsonl
      audit/
        YYYY-MM-DD.md
```

含义如下：

- `raw/YYYY-MM-DD.jsonl`
  追加式记录提取出的记忆事件。
- `searchable/facts.jsonl`
  当前项目的聚合事实集合。
- `searchable/archive/YYYY-MM-DD.jsonl`
  可选的追加式变更账本，用于记录合并、冲突、降级和提升。
- `audit/YYYY-MM-DD.md`
  从机器记录渲染出的、便于人类审阅的每日输出。
- `MEMORY.md`
  只根据被提升的事实进行确定性重建。

Markdown 用于人工审阅。JSONL 是机器契约。

### 4. 数据模型

#### 4.1 Raw event

`MemoryEvent` 是 post-turn 捕获步骤写入的基本单元。

必需字段：

- `event_id`
- `project_path`
- `session_file`
- `jsonl_line_range`
- `observed_at`
- `role_window_hash`
- `candidate_text`
- `normalized_text`
- `category`
- `durability`
  取值：`durable`、`tentative`
- `signal_type`
  取值：`explicit`、`implicit`、`project_constraint`
- `evidence`
- `source_platform`
- `turn_id`
- `extraction_version`

规则：

- `MemoryEvent` 是 append-only。
- 它必须始终携带足够证据，以便回溯到 transcript。
- 它不应被原地改写。

#### 4.2 Searchable fact

`SearchableFact` 是聚合后的工作记忆层。

必需字段：

- `fact_id`
- `project_path`
- `canonical_text`
- `display_text`
- `category`
- `status`
  取值：`active`、`tentative`、`contradicted`、`demoted`、`archived`
- `support_count`
- `distinct_turn_count`
- `distinct_session_count`
- `first_observed_at`
- `last_observed_at`
- `explicit_signal`
- `project_constraint_signal`
- `source_event_ids`
- `token_index`
- `promotion_state`
  取值：`never`、`candidate`、`promoted`、`demoted`

规则：

- 对同一个规范化语义，只应存在一个活跃的聚合事实。
- 冲突不会删除历史，只会改变状态。
- `token_index` 是本地词法索引载荷。v1 中它不是向量 embedding。

#### 4.3 Promoted memory item

`PromotedMemoryItem` 不会作为长期主记录单独存储。
它是从 `SearchableFact` 派生出的一个确定性视图。

必需字段：

- `fact_id`
- `display_text`
- `category`
- `promotion_reason`
- `rank`

### 5. 端到端流程

```text
new transcript window
  -> candidate extraction
  -> write MemoryEvent to raw JSONL
  -> consolidate into SearchableFact
  -> recompute promotion set
  -> rebuild MEMORY.md
  -> on next prompt, inject MEMORY.md + small recall block
```

### 6. Post-turn 捕获规则

post-turn hook 只应处理自上一个 checkpoint 以来新增的 transcript 内容。

捕获流水线：

1. 加载新的 transcript 窗口。
2. 提取候选记忆。
3. 过滤明显噪声。
4. 将被接受的候选项写入 `raw`。
5. 将 raw events 聚合成 searchable facts。
6. 如有需要，重新计算 promotion 并重建 `MEMORY.md`。

在以下情况下接受写入 `raw`：

- 明确的用户偏好
- 稳定的工作流习惯
- 稳定的工具使用偏好
- 稳定的沟通偏好
- 稳定的项目规则或项目约束

在以下情况下拒绝写入 `raw`：

- 一次性的 bug 细节
- 临时 TODO
- 瞬时的分支或文件状态
- 重复的脚手架内容或工具噪声
- 依据薄弱的推测

重要规则：

- extractor 可以写入 `raw`。
- extractor 不能直接写入 `MEMORY.md`。

### 7. Searchable facts 的聚合规则

聚合会把多个 raw events 合并成一个 fact。

在以下情况下合并：

- `normalized_text` 匹配
- category 兼容
- 不存在明确冲突

在以下情况下不合并：

- 新事件否定了旧事件
- 旧 fact 过于宽泛，而新 fact 明显更具体但并不等价

状态规则：

- 当支持证据较弱时，初始状态为 `tentative`
- 当 fact 稳定到足以用于 recall 时，转为 `active`
- 当较新的证据发生冲突时，转为 `contradicted`
- 当它不应继续留在 `MEMORY.md` 中时，转为 `demoted`
- 只有在它已经过时、即使做 recall 也没有价值时，才转为 `archived`

v1 默认阈值：

- `explicit` signal：
  一个被接受的事件就可以创建一个 `active` fact
- `implicit` signal：
  要求 `support_count >= 2`
- `project_constraint` signal：
  只要一个足够强的事件清晰描述了稳定的仓库规则，就可以创建一个 `active` fact

### 8. `MEMORY.md` 的 promotion 规则

`MEMORY.md` 只用于那些始终值得注入的记忆。

满足以下任一条件时进行提升：

- 该 fact 是来自用户的明确、持久指令
- 该 fact 是稳定的项目约束
- 该 fact 是一个 `support_count >= 2` 的隐式偏好

在 v1 中，以下情况不自动提升：

- category 是 `other`
- status 不是 `active`
- 该 fact 明显只属于当前任务
- 该 fact 只出现过一次且不是 explicit

promotion 的类别优先级：

1. `explicit_request`
2. `communication`
3. `workflow`
4. `tooling`
5. `project_context`
6. `language`
7. `other`

预算策略：

- 硬上限维持在现有的 180 行以内。
- v1 的软目标是 `60-100` 行。
- 更强的 fact 应该把更弱的 fact 挤出去。

降级策略：

- 冲突 fact：立即降级
- 过期的 project-context fact：优先于用户偏好被降级
- 在预算压力下支持度低且较弱的 fact：优先降级

### 9. Searchable 层的 recall 规则

pre-turn recall 应该搜索 `SearchableFact`，而不是 `raw`。

recall 流程：

1. 基于以下内容构建一个简短查询：
   - 当前用户 prompt
   - 可选的最近一轮摘要
2. 对活跃的 searchable facts 做词法匹配搜索，匹配字段包括：
   - `canonical_text`
   - `display_text`
   - `token_index`
3. 按以下因素排序：
   - 文本匹配质量
   - 支持计数
   - 新近性
   - promotion 状态
4. 去掉已被 `MEMORY.md` 覆盖的内容
5. 注入一个小而有边界的 recall 区块

v1 的 recall 预算：

- 前 `3-5` 个 facts
- 每个 fact 应该是一条简短 bullet
- recall 区块总大小应足够小，能够舒适地与 `MEMORY.md` 并存

### 10. 逻辑模块

这些是逻辑模块。第一天并不需要拆成独立文件。

| 模块 | 职责 |
|------|------|
| `capture` | hook 入口、transcript 窗口切分、checkpoint 读写 |
| `extract` | 基于 LLM 的新增 turn 候选记忆提取 |
| `raw_store` | append-only 的 raw JSONL 写入与加载 |
| `consolidate` | 将 events 合并为 facts，处理冲突和支持计数 |
| `search` | 对 searchable facts 做本地词法检索 |
| `promote` | promotion 打分、demotion、按类别配额 |
| `compile_memory` | 对 `MEMORY.md` 做确定性重建 |
| `inject` | 基于 `MEMORY.md` 和 recall block 构建 pre-turn 上下文 |
| `audit` | 渲染便于人读的每日审阅 Markdown |
| `state` | 幂等、重放保护、增量 checkpoint |

### 11. 与当前仓库的关系

这套架构应该演进当前的 layered mode，而不是替换它。

保留：

- 仅项目级的 pilot 形态
- append-only 的证据思路
- 确定性的整理记忆重建
- 基于 checkpoint 的增量处理

变更：

- 让 `raw` 变成机器可读的 JSONL，而不是只有 Markdown
- 在 candidate extraction 和 `MEMORY.md` 之间增加真正的 `searchable` 层
- 把 “searchable recall” 和 “always inject memory” 分离开

### 12. v1 非目标

不在范围内：

- 全局记忆
- 向量搜索
- 跨项目去重
- mem0 集成
- 聚合后依赖 LLM 做自动冲突消解
- 重型知识图谱能力

## 需要支持

这份文档之后的实现工作应按以下顺序进行：

1. 引入 `MemoryEvent` JSONL 写入。
2. 引入 `SearchableFact` 聚合。
3. 仅根据被提升的 facts 重建 `MEMORY.md`。
4. 从 searchable facts 增加 pre-turn recall block。
5. 增加 audit Markdown 渲染。

实现期间建议验证的问题：

- 在很多轮之后，`MEMORY.md` 还能保持简短吗？
- 一个有用 fact 能否存在于 searchable 中而不污染 `MEMORY.md`？
- 每一条被提升的内容都能追溯到 raw evidence 吗？
- 对同一个窗口重复运行时，state 能保持幂等吗？

## 关键信息

这个仓库不应该试图让 `MEMORY.md` 承载全部记忆。

它的职责是把三层管理清楚：

- `raw` 保存证据
- `searchable` 保存工作记忆
- `MEMORY.md` 只保留那小部分始终值得注入的事实

这是目前最简单、同时又能支持以下目标的架构：

- 持续增长的记忆
- 有边界的原生记忆
- 可解释的提升与降级
- 在不丢失当前产品方向的前提下支持未来后端升级
