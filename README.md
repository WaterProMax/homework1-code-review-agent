# Review Studio · 代码审查工作台

Homework 1 的代码审查 Agent：面向 Python 小型项目，支持 CLI 和本地浏览器工作台。

工作流程：**加载项目 → 选择文件 → 工具检查与 Agent 分析 → 查看证据 → 修复预览 → 手动修改 → 复查 → 导出报告**。

## 快速启动工作台

需要 Python 3.11+、Node.js 22.12+（建议 Node.js 24）和 npm。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[web,dev]"
cp .env.example .env
```

在 `.env` 配置模型，密钥只由 Python 后端读取：

```env
DEEPSEEK_API_KEY=your_key
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat
```

```bash
cd frontend
npm ci
npm run build
cd ..
code-review-agent serve --host 127.0.0.1 --port 8000
```

打开 **http://127.0.0.1:8000**。输入本地项目目录或 `.py` 文件路径，也可以点击“试用演示项目”。服务在前台持续运行，按 Ctrl+C 停止。

未配置密钥时，工作台仍可进行静态检查和部分规则的本地修复预览，报告会明确标为“部分完成”。模型审查、追问和其他类型的修复预览需要有效密钥。实际启用模型时，选中的源码和相关上下文会发送到配置的模型 API。`requirements.lock` 记录本次验证的 Python 依赖版本，可用 `pip install -e ".[web,dev]" -c requirements.lock` 约束安装。

## 工作台能力

- Python 文件发现、勾选、源码行号、问题位置高亮。
- 每个文件独立执行静态预检查与 ReAct 循环，汇总目录结果。
- 问题包含位置、证据、来源、证据程度、建议和处理状态。
- 记录实际文件检查、工具调用、观察和汇总过程。
- 针对问题进行追问，展示只读修改前后差异。
- 复制修改后代码、下载补丁；服务不会自动修改待审查源码。
- 手动修改后复查，区分仍存在、已解决、新问题和无法确认。
- 审查历史保存当时的源码快照，旧报告不随文件变化而改写。
- 导出 Markdown 报告和包含快照的 JSON 数据。
- 页面刷新恢复最近项目及正在执行的任务；服务重启后未完成任务标记中断。
- 单次最多 20 个文件、单文件 80 KB、源码总量 1 MB；超限明确提示。

`confirmed` 表示工具已证实，`resolved` 表示复查确认解决，两者不是一回事。模型发现默认待确认；未找到问题也不等于不存在其他缺陷。文件删除、作用域丢失或证据不足时不会自动关闭问题。

## CLI 使用

```bash
# 单文件，保留原有使用方式
code-review-agent --session demo review examples/buggy.py 只看正确性和资源问题

# 目录审查，使用项目工作台会话和统一审查服务
code-review-agent review examples/demo_project

# 原有 CLI 会话追问、记忆、清空
code-review-agent --session demo chat "刚才那个高危怎么改？"
code-review-agent --session demo chat
code-review-agent --session demo memory
code-review-agent --session demo reset --yes
```

CLI 单文件会话保存在 `.code_review_agent/sessions/`；工作台和 CLI 目录审查共用项目会话，保存在 `.code_review_agent/web/`。单文件 CLI 的 `--session` 不会切换工作台的项目会话。REPL 支持 `:memory` 和 `:quit`。

## 开发模式

先在项目根启动后端；再开一个终端：

```bash
cd frontend
npm ci
npm run dev
```

访问 **http://127.0.0.1:5173**，Vite 将 `/api` 代理到本机 8000 端口。前端开发模式后端使用默认 8000 端口；交付模式可以通过 `serve --port` 修改端口。

Monaco 编辑器及 worker 已打包到本地，不依赖在线 CDN。前端使用 React、TypeScript、Vite 和 React Markdown，后端使用 FastAPI、Uvicorn 与现有 OpenAI 兼容 SDK。HTTP 客户端包含 SOCKS 支持，可使用已有代理配置。

## 结构

```text
code_review_agent/
  agent.py             ReAct 循环与执行事件
  analysis.py          确定性 AST 规则
  tools.py             文件、静态检查、片段、问题登记、记忆工具
  memory.py            分层记忆和旧会话兼容
  review_service.py    项目、目录任务、快照、修复预览、复查、报告
  api.py               本机 HTTP 接口与静态前端服务
  cli.py               CLI 与 serve 入口
frontend/              React 工作台、单元测试和浏览器验收
examples/demo_project/ 带缺陷的三文件演示项目
examples/fixed_project/ 对应的修复参考
scripts/               真实模型验收及提交打包工具
tests/                 后端测试和人工标注样例
Design.md              技术设计
Desgin.md              PPT 指定拼写的文档入口
docs/                  验证记录与一分钟演示脚本
```

## 测试

```bash
pytest
cd frontend
npm test
npm run build
npx playwright install chromium
npm run test:e2e
```

浏览器测试自动启动隔离测试服务，使用 Fake LLM，静态检查和文件读写是真实执行；8001 端口需要空闲。它只修改临时项目，不修改演示源码。

真实模型验收在项目根运行：

```bash
python scripts/validate_live.py
```

需要有效密钥与网络；在临时项目副本上完成三文件审查、追问、预览和定向复查，结果写到被忽略的 `.code_review_agent/validation/`。

## 边界与常见问题

| 情况 | 行为 / 处理 |
|---|---|
| 首页提示尚未构建 | 在 `frontend` 执行 `npm ci`、`npm run build` |
| 路径不存在、编码不支持、文件超限 | 明确报错，其他文件可以继续检查 |
| 模型超时或拒绝请求 | 有限次重试，保留工具证据，报告标为部分完成 |
| 文件已变化 | 查看快照提示，复查后再生成新的修复预览 |
| 复查无法确认 | 保留待处理状态，展示验证说明 |
| 已有任务运行 | 新任务返回忙碌提示，避免并发改写会话 |
| 非本机网页访问 | Host 和 Origin 校验拒绝请求 |

本项目按文件完成分析与项目汇总，不提供完整跨文件调用图或数据流分析。资源关闭检查是 AST 启发式规则，不证明所有路径都正确释放资源。`run_snippet` 拒绝导入、I/O 和动态执行，使用临时目录、空白环境及资源上限，但不是通用容器沙箱。模型修复预览校验原片段与 Python 语法，仍需用户确认业务语义。

## 提交

```bash
python scripts/package_submission.py
```

生成学号姓名压缩包，排除真实 `.env`、运行记忆、虚拟环境、`node_modules`、旧压缩包和浏览器测试缓存；包含前端构建结果，解压安装后端即可运行工作台。完整设计见 [Design.md](Design.md)，验证记录见 [docs/Validation.md](docs/Validation.md)，演示脚本见 [docs/Demo.md](docs/Demo.md)。
