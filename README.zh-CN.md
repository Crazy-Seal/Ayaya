[**English**](./README.md) | [**简体中文**](./README.zh-CN.md)

# Ayaya

Ayaya 是一个面向 Windows 的本地 AI 桌宠。Live2D 角色待在桌面上，通过 OpenAI 兼容接口和你聊天，记住之前的对话，可以接收图片，并在你授权的范围内用一组本地工具操作电脑。

所有内容都留在你自己的机器上：聊天记录、长期记忆、导入的模型和设置都是本地文件。项目没有账号体系，不是在线托管服务，也不依赖远程数据库。

## 它能做什么

- **桌宠外观。** 显示 Live2D 角色，位置、缩放、视线跟随和动作标签都可以调整。
- **流式聊天。** 模型还在生成时，回复就已经逐段显示在聊天面板里。
- **长期记忆。** 日记记忆、情景记忆和语义记忆可以按会话分别开关，并调整抽取节奏。
- **图片输入。** 可以把截图或图片文件发给具备视觉能力的聊天模型。
- **本地工具。** 读写与修改工作区文件、运行常驻 Shell、使用常驻 Python REPL、联网搜索，并维护一份可见的计划列表。
- **子 Agent。** 主 Agent 可以创建子 Agent，在子 Agent 运行期间继续引导它，并收取最终报告。
- **插件。** 内置 `memory` 和 `computer_use` 插件，也支持外部插件包。
- **MCP 服务。** 可以接入 stdio 或 streamable HTTP 的 MCP 服务，把它们的工具交给 Agent 使用。
- **按会话授权。** 每个工具在每个会话里都可以设为 `allow`、`ask` 或 `deny`。

## 运行要求

- Windows 作为开发和运行平台。
- Python 3.12，位于名为 `ayaya` 的 Conda 环境中。
- Node.js 24.x。
- 一个 OpenAI 兼容的聊天模型接口。
- 可选：用于 embedding 和联网搜索的服务密钥。

## 快速开始

先安装后端依赖：

```powershell
conda run -n ayaya pip install -r requirements.txt
```

然后安装并启动桌面端：

```powershell
cd ui
npm install
npm run dev
```

Electron 会在 `http://127.0.0.1:8000` 启动后端，等待后端就绪，并在桌面端退出时关闭后端。

首次运行前端前，需要下载默认 Live2D 模型：

```text
https://cubism.live2d.com/sample-data/bin/hiyori_pro/hiyori_pro_zh.zip
```

把 `hiyori_pro_zh.zip` 直接放到 `ui/` 目录下。前端准备脚本会把它解压到 `ui/public/live2d/`，并在需要时下载 Cubism Core。启动后，你可以在设置中导入其他自己的 Live2D 模型。

> [!NOTE]
> `npm run dev` 和 `npm run start` 已经会管理后端。默认流程不要再单独启动后端；只有在调试后端本身时，才使用[开发](#开发)一节里的独立启动方式。

## 配置

打开桌面端的设置窗口，配置模型 API、角色提示词、工具权限、记忆参数、Live2D 模型、表情标签等。应用会把你的选择写入本地配置文件：

| 路径 | 内容 |
| --- | --- |
| `config/chat_settings.yaml` | 每个会话的模型、提示词、工具权限、插件和上下文设置 |
| `config/mcp_servers.yaml` | MCP 服务连接 |
| `.env` | embedding、记忆抽取和搜索的服务密钥 |

模板文件是 `config/chat_settings.sample.yaml`、`config/mcp_servers.sample.yaml` 和 `.env.sample`。

部分可选功能需要在项目根目录 `.env` 中补充密钥：

```dotenv
# 长期记忆 embedding
EMBEDDING_API_KEY=YOUR_API_KEY
EMBEDDING_MODEL=text-embedding-v4
EMBEDDING_DIMENSION=1024
EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1

# 情景记忆抽取：请选择支持结构化 JSON 输出的接口
EPISODIC_EXTRACTION_MODEL=qwen3.8-flash
EPISODIC_EXTRACTION_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
EPISODIC_EXTRACTION_API_KEY=YOUR_API_KEY

# 语义记忆抽取：请选择按约定输出 JSON 对象的接口
MEM0_EXTRACTION_MODEL=qwen3.8-flash
MEM0_EXTRACTION_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
MEM0_EXTRACTION_API_KEY=YOUR_API_KEY

# 记忆抽取时本地估算上下文的上限
MEMORY_EXTRACTION_MAX_CONTEXT_TOKENS=128000

# 联网搜索
TAVILY_API_KEY=YOUR_API_KEY
```

> [!TIP]
> 优先配好聊天模型即可，其余密钥可在启用对应功能时添加。

## 工具与权限

Agent 可以调用以下内置工具。会话启用插件和 MCP 服务后，它们的工具会追加在这份列表之上。

| 工具 | 作用 |
| --- | --- |
| `read_file` | 读取工作区文件或已保存的工具结果 |
| `apply_patch` | 用一份补丁新增、修改或删除工作区文件 |
| `read_image` | 读取工作区图片并作为工具图片返回 |
| `exec_command` | 在自动探测的 Shell 中启动受管任务 |
| `write_stdin` | 向运行中的任务写入 stdin，或读取它的新输出 |
| `python` | 在应用内常驻 REPL 中执行 Python |
| `python_reset` | 重置 REPL 状态，包括变量和导入 |
| `update_plan` | 维护可见的计划列表 |
| `access_the_internet` | 通过 Tavily 联网搜索 |
| `create_subagent` | 创建带初始任务的子 Agent |
| `interact_subagent` | 向子 Agent 发送命令、停止或销毁它 |
| `get_subagent_details` | 查看子 Agent 的定义、交互记录和报告 |

每个工具在每个会话里都有一条 `allow`、`ask` 或 `deny` 策略，保存在 `config/chat_settings.yaml` 的 `tool_permissions` 下。设为 `ask` 时，Agent 会暂停，等你在桌面端做出决定后再执行。

## 插件与 MCP 服务

内置插件位于 `app/agent/plugins/`：

- `memory` 提供 `search_memory` 和 `search_diary`，并驱动日记、情景和语义记忆的抽取。
- `computer_use` 把 `ayaya_computer_use` 辅助模块注入 Python REPL，让模型可以查看窗口、控件树和画面，并对本机进行控制。

启动时会从项目根目录的 `plugins/` 发现外部插件包。插件包、配置、工具、MCP 服务、钩子和后台任务的写法见[插件开发指南](docs/plugin-development.md)。

MCP 服务在 `config/mcp_servers.yaml` 中配置，支持 `stdio` 和 `streamable_http` 两种传输方式，并可为每个服务设置连接和调用超时。

## 仓库结构

```text
main.py                 FastAPI 入口与 lifespan 装配
app/
  routes/               HTTP 与流式接口
  services/             聊天、设置、历史、插件、MCP 等应用服务
  schemas/              Pydantic 请求、响应与领域模型
  crud/                 SQLite 持久化与迁移支撑
  agent/
    agent.py            Agent 实现
    runtime/            会话、执行、Scheduler、Inbox、审批
    tools/              内置工具
    plugins/            内置 memory 与 computer_use 插件
    models/             模型与 embedding 客户端
    memory/             长期记忆
    context/            上下文预算与压缩
    state/              会话状态与消息协议
  interaction/          交互契约与事件 Hub
  mcp/                  MCP 连接管理
  repl/                 常驻 Python REPL
  security/             本地 API Token 校验
  processes/            Windows Job 对象
  environment.py        存储路径与环境模式
ui/
  electron/             主进程、后端生命周期、IPC
  src/                  聊天、Live2D、设置和日志的渲染进程代码
  public/               前端静态资源
  user_data/            运行时导入的模型和前端设置
scripts/                离线迁移脚本
tests/                  后端 pytest 套件
```

## 开发

独立调试后端时，两个终端必须使用同一个临时 Token。不要持久化或提交这个 Token：

```powershell
# 终端 1
$env:AYAYA_API_TOKEN="<至少 43 字符的临时 Base64URL Token>"
conda run -n ayaya python -B -m app.server

# 终端 2
$env:AYAYA_API_TOKEN="<同一个临时 Token>"
$env:AYAYA_MANAGE_BACKEND="false"
cd ui
npm run dev
```

以下环境变量可用于开发覆盖：

- `AYAYA_PYTHON_EXECUTABLE`：指定 Python 可执行文件，替代自动发现的 `ayaya` Conda 环境。
- `AYAYA_BACKEND_CWD`：修改 Electron 启动后端时使用的工作目录。
- `AYAYA_BACKEND_BASE_URL`：让 Electron 连接其他回环地址上的后端；外部后端模式仍必须提供匹配的 `AYAYA_API_TOKEN`。

Electron 通过 Conda 的 JSON 环境列表查找名为 `ayaya` 的环境，并直接启动该环境的 Python，因此长期运行的后端进程树里不含 `conda run`。

面向贡献者和编码 Agent 的仓库约定见 [AGENTS.md](AGENTS.md)。

## 构建

```powershell
cd ui
npm run build
npm run start
```

`npm run build` 会依次执行 Live2D 准备、TypeScript 检查和 Vite 生产构建，`npm start` 启动构建后的应用。

## 测试

后端测试必须使用隔离的测试目录：

```powershell
$env:AYAYA_ENV="test"
$env:AYAYA_DATA_DIR="$env:TEMP\ayaya-test-data"
conda run -n ayaya python -B -m pytest tests -q -p no:cacheprovider
```

测试模式不会加载生产 `.env`，所有 SQLite、checkpoint、向量索引和图片都必须留在 `AYAYA_DATA_DIR` 内。

前端检查：

```powershell
cd ui
npm run typecheck
npm run test:unit
```

## 本地数据

正常本地运行会使用：

- `memory/` 保存后端运行数据：SQLite 数据库、checkpoint、图片和记忆存储。
- `config/chat_settings.yaml` 和 `config/mcp_servers.yaml` 保存会话和 MCP 设置。
- `ui/user_data/` 保存导入的 Live2D 模型、模型元数据、前端设置和模型变换数据。

凭据和生成的运行数据不会进入版本控制。`.env`、本地配置文件、`memory/`、`agent_workspace/` 和 `ui/user_data/` 都由 `.gitignore` 排除。
