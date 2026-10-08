# 设计文档

本文是 Homework 1 的技术设计说明。作业 PPT 中的文件名写作 `Desgin.md`，该文件仅作入口，正文以本文件为准。

## 1. 项目目标与选型

作业要求搭建一个简单的代码助手 Agent，掌握 LLM 调用、Prompt 设计、工具集成。本项目选择 **代码审查 Agent**：

- 输入可以是文件路径或追问
- 输出必须指出质量问题 / 潜在 Bug，并给出可执行建议
- 结论必须能回溯到工具观察，避免“套一层 ChatGPT”

不选代码生成作为主方向，是因为生成很容易退化成单次 completion，Agent 架构分不容易拉开。审查天然需要取证：先读文件、再静态分析、必要时跑最小片段，最后才下结论。

### 1.1 对照作业技术要求

| 作业要求 | 本项目怎么做 |
|----------|----------------|
| Agent 循环：输入 → 推理 → 工具调用 → 输出 | `CodeReviewAgent.run()` 手写 ReAct 循环 |
| 至少一种工具 | v0.1 为 5 个工具；v0.2 增加结构化问题登记工具 |
| 命令行或 Web 交互 | CLI：`review` / `chat` / `memory` / `reset` |
| 主流框架或原生 LLM API | DeepSeek 原生 API（OpenAI 兼容） |
| 上下文记忆 | 工作 / 情节 / 语义 / 会话持久化，外加 compact |
| 错误处理与重试 | LLM 退避重试、工具结构化报错、步数上限、降级总结 |

### 1.2 技术栈

- 语言：Python 3.11+
- LLM：DeepSeek `deepseek-chat`，`openai` SDK
- 依赖：`openai`、`python-dotenv`；开发依赖 `pytest`
- 不引入 LangChain / AutoGen / CrewAI：作业规模下，手写循环的模块边界更清晰，也更容易在文档里画清楚

## 2. 总体架构

```text
CLI (cli.py)
  └─ CodeReviewAgent.run()          # agent.py
        ├─ MemoryStore.working_messages()
        ├─ LLMClient.complete()     # tool calling + retry
        ├─ Toolbelt.execute()       # 观察写回 working / semantic
        └─ MemoryStore.save()       # sessions/<id>.json
```

### 2.1 模块职责

| 模块 | 职责 |
|------|------|
| `cli.py` | 参数解析、会话加载、人机交互 |
| `agent.py` | ReAct 循环、终止条件、偏好抽取、episode 记录、LLM 失败降级 |
| `memory.py` | 四层记忆、token 预算、compact、会话读写 |
| `tools.py` | 工具 schema 与执行；文件/AST/snippet/记忆 |
| `llm.py` | OpenAI 兼容客户端，超时 / 429 / 5xx 重试 |
| `prompts.py` | 审查合同 + 记忆压缩提示词 |
| `config.py` | `.env` 与运行上限 |

### 2.2 一次审查的时序

```text
用户: review examples/buggy.py 只看正确性和资源问题
  → 写入 preference，append user message
  → 必要时 compact
  → loop:
       LLM 看到 system + memory overlay + 最近对话
       若 tool_calls: 执行工具，观察 append 进 messages，继续
       若最终文本: 输出四段式 Markdown，记 episode，save，结束
  → 超过 max_steps=8: 基于已有观察强制总结
```

终止条件：

1. 模型不再发起 tool call，给出最终中文 Markdown
2. LLM 连续失败，降级为“仅基于已有工具观察”
3. 达到 `max_steps`（默认 8），强制阶段性总结

## 3. 上下文记忆

如果只把 `messages[-N:]` 拼回去，多轮审查会有三个问题：

- 源码和工具输出很快撑爆窗口，模型开始忘掉 findings
- 用户说“那个高危怎么改”时，Agent 不知道“那个”是哪一条
- 进程一退出，下一节课的演示接不上

所以记忆分成四层，职责不同。

### 3.1 Working Memory（工作记忆）

当前真正送进模型的窗口：

1. 固定 system prompt
2. 一份 **memory overlay**（从长期记忆投影出来的短摘要）
3. 最近 `recent_turns_keep` 轮对话，包含 tool 观察

它是容量受限的。token 粗估超预算时，不会直接丢弃审查结论，而是先 compact。

### 3.2 Episodic Memory（情节记忆）

每一次 `run()` 结束都会记一条 episode：

- 用户目标
- 碰过哪些文件
- 用过哪些工具
- 一句话结果
- 关联 finding id

这是“上一轮发生了什么”，用来回答“你刚才审查过 buggy.py 吗”“上次除零问题还在吗”。

### 3.3 Semantic Memory（语义记忆）

跨轮稳定知识，不依赖某次对话原文：

| 类型 | 例子 |
|------|------|
| preferences | 只看高危、关注异常处理 |
| file facts | 路径、大小、sha256、一行摘要 |
| findings | id、严重级别、位置、状态（open/confirmed/resolved/invalid） |
| notes | 已被推翻的旧结论、用户确认过的事实 |

findings 有状态，所以用户说“可变默认参数我改掉了”，可以用 `remember(kind=finding, status=resolved)` 或下一轮工具复查后更新，而不是在聊天记录里原地打补丁。

### 3.4 Session Persistence（会话持久化）

以上状态序列化到 `.code_review_agent/sessions/<session>.json`。CLI `--session demo` 下次启动会直接 `MemoryStore.load()`。作业规模下结构化 JSON 比 embedding RAG 更可控，也更好写进测试。

### 3.5 Compact（压缩）

当 working window 超出预算：

1. 先用 LLM 把旧对话压成 `[EPISODIC] / [SEMANTIC] / [DROP]`
2. LLM 失败则走本地 fallback digest
3. 完整源码和重复工具输出进入 DROP
4. 原始 messages 裁到最近几轮，overlay 继续携带 findings

因此“记忆”不是越积越长的 chat log，而是 **可检索的审查状态机**。

### 3.6 记忆相关工具

模型可以主动操作记忆，而不是被动被灌上下文：

- `remember`：写入偏好、稳定结论、finding 状态
- `recall_memory`：按文件名 / finding id / 关键词检索，避免重复 `read_file`

## 4. 工具设计

| 工具 | 证据类型 | 为什么需要 |
|------|----------|------------|
| `read_file` | 带行号的源码 | 没有正文就禁止虚构行号 |
| `parse_python` | AST 问题列表 | 可变默认参数、裸 except、除零、未使用导入、open 未 with |
| `run_snippet` | 运行期异常 | 把“可能崩”变成“这段确实崩了”；先做 AST 白名单校验，再在空白环境、临时目录和资源上限下执行 |
| `remember` / `recall_memory` | 记忆读写 | 让架构里的记忆层可被 Agent 循环使用 |

`parse_python` 发现的高置信问题会立刻写入 findings，来源标记为 `tool`。最终报告要求区分“已证实”和“仅怀疑”。

`run_snippet` 只接受无导入、无 I/O 的纯 Python 最小片段。它先用 AST 白名单拒绝文件、网络、进程和动态执行入口，再在不继承主进程环境变量的子进程中执行，并限制超时、文件大小和打开文件数。它仍不是通用容器沙箱。

## 5. Prompt 策略

System prompt 把合同写死：

- 角色：Python 代码审查 Agent，必须 ReAct
- 无观察不虚构文件内容、行号或运行结果
- 有路径先 `read_file`，需要结构再 `parse_python`
- 只有静态检查不够时才 `run_snippet`
- 追问优先用记忆，不要无故重读整个项目
- 输出固定四段：摘要 / 问题列表 / 改进建议 / 工具与记忆依据
- 证据不足要承认“仅怀疑”

Compact 另用一套 summarizer prompt，明确禁止发明新 bug，并要求把可丢弃的完整源码标进 `[DROP]`。

## 6. 错误处理与边界

- 配置：缺 Key 直接退出，不发起请求
- 文件：不存在、超大、非 UTF-8 由工具返回结构化错误
- LLM：超时 / 429 / 5xx 有限次退避重试；仍失败则基于已有 findings 降级
- 工具参数：非法 JSON、未知工具、签名不匹配都返回 `ok: false`
- 步数：防止 tool calling 死循环

| 边界 | 行为 |
|------|------|
| 空输入 | 不调用模型 |
| 文件不存在 | 工具报错，Agent 说明下一步 |
| 超大文件 | 拒绝读取并提示拆分 |
| 语法错误 | `parse_python` 作为高危 finding |
| 用户追问 | overlay + recall，不强制重读 |
| 会话重启 | JSON 恢复 findings / preferences |

## 7. v0.1 范围（v0.2 扩展见第 8 节）

- 不做多 Agent / LangChain 编排，避免把作业做成框架演示
- 不做全仓库索引或 embedding RAG
- 不做 Web UI
- `run_snippet` 不是通用容器沙箱，只验证通过 AST 白名单的纯 Python 最小片段

## 8. v0.2 工作台扩展

原设计中的“仅单文件、不做 Web UI”描述的是 v0.1 的边界。v0.2 已增加目录审查与本地浏览器界面；旧 CLI 使用方式仍可用。本版不增加多 Agent、账户系统、文件上传或完整跨文件依赖分析。

### 8.1 前后端与任务

React + TypeScript + Vite 构建只读代码工作台；Monaco 编辑器、worker 和语言资源本地打包。FastAPI 提供 `/api` 接口与构建后的静态资源。生产演示只启动一个本地 Python 服务，开发时 Vite 代理 API。

`ReviewService` 管理项目路径、文件选择、项目会话、单工作线程任务与结果持久化。后台任务避免阻塞 HTTP 请求，前端按序号轮询增量事件。并发新任务返回 409；服务重启将未完成任务标记为 `interrupted`。

### 8.2 HTTP 接口

| 接口 | 说明 |
|---|---|
| GET `/api/health` | 模型配置状态、限制、演示项目路径，不返回密钥 |
| POST `/api/projects` | `{path}`，加载本地目录或 Python 文件 |
| GET `/api/projects/{id}/files` | 文件列表、大小、可用状态 |
| GET `/api/projects/{id}/source?path=...` | 项目相对路径的源码与版本 |
| POST `/api/sessions` | `{project_id,new?}`，恢复或新建会话 |
| GET `/api/sessions/{id}` | 历史记录、追问与记忆摘要 |
| POST `/api/sessions/{id}/jobs` | `{kind,paths,instruction,run_id,finding_id}` |
| GET `/api/jobs/{id}?after_seq=...` | 任务状态、进度和新增事件 |
| GET `/api/runs/{id}` | 结果与当时的完整源码快照 |
| GET `/api/runs/{id}/report?format=md或json` | 报告下载 |

任务类型为 `review/recheck/chat/fix`。任务状态为 `queued/running/completed/partial/failed/interrupted`。复查可传问题编号来限定到对应文件；不传则复查原始范围。

### 8.3 工具与结构化结果

增加 `record_finding`：模型必须提交已读取源码中的位置、证据和建议，服务校验后仅登记为待确认。前端直接消费结构化问题，不解析最终 Markdown。已有 `remember` 在工作台中不允许直接关闭问题。

`Finding` 增加 `code/scope/fingerprint/confidence/end_line/version`，旧 JSON 会话用默认值加载。静态问题指纹包含规则、所属作用域、问题内容和代码特征，区分同文件多处问题，并支持仅行号移动时的匹配。

工具输出过长时返回有效的截断 JSON，完整 AST 发现仍保存在记忆。源码上下文截断会在报告中标明模型覆盖有限，不伪装成完整审查。

### 8.4 快照、修复与复查

每个审查文件先读取一次并保存 SHA-256 版本。静态检查和模型工具读取同一快照；工具只允许本次已选文件，避免混入其他文件或运行中发生的源码变化。历史记录保存独立副本，不随后续状态改变。

修复预览绑定原版本。可变默认参数提供确定性的本地预览，其他问题由模型返回唯一原片段与替换片段，服务验证匹配、生成 unified diff 并检查 Python 语法。页面只读、不会自动写入待审查源码。

复查读取当前版本，对照旧问题：指纹精确匹配优先，同规则、作用域、标题的唯一候选可进行保守匹配。对应作用域存在、解析成功且规则确认原问题消失时才关闭；文件删除、作用域丢失、模型怀疑或匹配歧义均保留待确认。

### 8.5 边界与存储

每次最多 20 个文件、单文件 80 KB、总量 1 MB。排除环境、Git 和生成目录；解析真实路径后拒绝越界符号链接；源码 API 仅读取项目范围内 Python 文件。服务仅绑定 loopback，并检查 Host 与 Origin。

工作台状态和任务事件存储为 `.code_review_agent/web/state.json`，会话记忆与审查记录分别保存到该目录下 `memory/` 和 `runs/`。使用临时文件替换完成 JSON 原子写入。CLI 目录审查共用这个服务，原单文件 CLI 会话保持在 `sessions/`。

默认不配置模型也能启动 Web 服务并完成静态检查，结果标记 `partial`。实际启用模型时，源码上下文会发送到配置的模型 API。静态规则是有限启发式检查，不是完整 linter 或数据流证明。
