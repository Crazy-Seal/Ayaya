[**English**](./README.md) | [**简体中文**](./README.zh-CN.md)

# Ayaya

Ayaya is a local AI desktop pet for Windows. A Live2D character sits on your desktop, chats through an OpenAI-compatible model, remembers earlier conversations, accepts images, and can act on your computer through a set of permissioned local tools.

Everything stays on your machine: chat history, long-term memory, imported models and settings are all local files. There is no account system, no hosted service and no remote database.

## What It Does

- **Desktop companion.** Renders a Live2D character with configurable position, scale, cursor tracking and motion labels.
- **Streaming chat.** Replies stream into the chat panel while the model is still generating.
- **Long-term memory.** Diary, episodic and semantic memory, each switchable per session, with a configurable extraction cadence.
- **Image input.** Send screenshots or image files to vision-capable chat models.
- **Local tools.** Read and patch workspace files, run a persistent shell, drive a persistent Python REPL, search the web, and keep a visible plan.
- **Sub-agents.** The main agent can create child agents, steer them while they run, and collect their final reports.
- **Plugins.** Built-in `memory` and `computer_use` plugins, plus external plugin packages.
- **MCP services.** Attach stdio or streamable HTTP MCP servers and expose their tools to the agent.
- **Per-session permissions.** Every tool is set to `allow`, `ask` or `deny` for each chat session.

## Requirements

- Windows as the development and target platform.
- Python 3.12 in a Conda environment named `ayaya`.
- Node.js 24.x.
- An OpenAI-compatible chat model endpoint.
- Optional service keys for embeddings and web search.

## Quick Start

Install the backend dependencies:

```powershell
conda run -n ayaya pip install -r requirements.txt
```

Then install and start the desktop app:

```powershell
cd ui
npm install
npm run dev
```

Electron starts the backend on `http://127.0.0.1:8000`, waits for the backend to become ready, and shuts the backend down when the desktop app exits.

Before the first frontend run, download the default Live2D model:

```text
https://cubism.live2d.com/sample-data/bin/hiyori_pro/hiyori_pro_zh.zip
```

Put `hiyori_pro_zh.zip` directly inside `ui/`. The frontend preparation script extracts it into `ui/public/live2d/` and downloads Cubism Core when needed. After the app is running, you can import your own Live2D models from the settings window.

> [!NOTE]
> `npm run dev` and `npm run start` already manage the backend. Do not start a second backend for the default workflow; use the standalone setup under [Development](#development) only when working on the backend itself.

## Configuration

Open the settings window in the desktop app and configure the model API, the character prompt, tool permissions, memory settings, the Live2D model, expression labels and more. The app writes these choices to local config files:

| Path | Contents |
| --- | --- |
| `config/chat_settings.yaml` | Per-session model, prompt, tool permissions, plugin and context settings |
| `config/mcp_servers.yaml` | MCP server connections |
| `.env` | Service keys for embeddings, memory extraction and search |

Templates are available as `config/chat_settings.sample.yaml`, `config/mcp_servers.sample.yaml` and `.env.sample`.

Some optional features need keys in the root `.env` file:

```dotenv
# Long-term memory embeddings
EMBEDDING_API_KEY=YOUR_API_KEY
EMBEDDING_MODEL=text-embedding-v4
EMBEDDING_DIMENSION=1024
EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1

# Episodic memory extraction: use an endpoint that returns structured JSON
EPISODIC_EXTRACTION_MODEL=qwen3.8-flash
EPISODIC_EXTRACTION_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
EPISODIC_EXTRACTION_API_KEY=YOUR_API_KEY

# Semantic memory extraction: use an endpoint that returns JSON objects
MEM0_EXTRACTION_MODEL=qwen3.8-flash
MEM0_EXTRACTION_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
MEM0_EXTRACTION_API_KEY=YOUR_API_KEY

# Upper bound used for the local context estimate of memory extraction
MEMORY_EXTRACTION_MAX_CONTEXT_TOKENS=128000

# Web search
TAVILY_API_KEY=YOUR_API_KEY
```

> [!TIP]
> Configure the chat model first; the remaining keys can be added when you enable the corresponding features.

## Tools and Permissions

The agent can call the following built-in tools. Plugin and MCP tools are added on top of this list when the session enables them.

| Tool | What it does |
| --- | --- |
| `read_file` | Read a workspace file or a stored tool result |
| `apply_patch` | Add, modify or delete workspace files in one patch |
| `read_image` | Read a workspace image and return it as a tool image |
| `exec_command` | Launch a managed task in the auto-detected shell |
| `write_stdin` | Write to a running task's stdin or read its new output |
| `python` | Run Python in the application's persistent REPL |
| `python_reset` | Reset the REPL state, including variables and imports |
| `update_plan` | Maintain the visible plan list |
| `access_the_internet` | Search the web through Tavily |
| `create_subagent` | Create a child agent with an initial task |
| `interact_subagent` | Command, stop or destroy a child agent |
| `get_subagent_details` | Inspect a child agent's definition, interactions and report |

Each tool gets a policy of `allow`, `ask` or `deny` per session, stored under `tool_permissions` in `config/chat_settings.yaml`. With `ask`, the agent pauses and waits for your decision in the desktop app before the call runs.

## Plugins and MCP Services

Built-in plugins live in `app/agent/plugins/`:

- `memory` exposes `search_memory` and `search_diary`, and drives diary, episodic and semantic extraction.
- `computer_use` injects the `ayaya_computer_use` helper into the Python REPL so the model can inspect windows, accessibility trees and screen captures, and control the computer.

External plugin packages are discovered from a `plugins/` directory at the project root at startup. See the [plugin development guide](docs/plugin-development.md) for packaging, configuration, tools, MCP services, hooks and background tasks.

MCP servers are configured in `config/mcp_servers.yaml` with `stdio` or `streamable_http` transport, plus per-server connect and call timeouts.

## Repository Layout

```text
main.py                 FastAPI entry point and lifespan wiring
app/
  routes/               HTTP and streaming endpoints
  services/             Application services for chat, settings, history, plugins, MCP
  schemas/              Pydantic request, response and domain models
  crud/                 SQLite persistence and migrations support
  agent/
    agent.py            Agent implementation
    runtime/            Sessions, executions, scheduler, inbox, approvals
    tools/              Built-in tools
    plugins/            Built-in memory and computer_use plugins
    models/             Model and embedding clients
    memory/             Long-term memory
    context/            Context budgeting and compaction
    state/              Session state and message protocol
  interaction/          Event contracts and the event hub
  mcp/                  MCP connection management
  repl/                 Persistent Python REPL
  security/             Local API token enforcement
  processes/            Windows job objects
  environment.py        Storage paths and environment mode
ui/
  electron/             Main process, backend lifecycle, IPC
  src/                  Renderer code for chat, Live2D, settings and logs
  public/               Static frontend assets
  user_data/            Imported models and frontend settings at runtime
scripts/                Offline migration scripts
tests/                  Backend pytest suite
```

## Development

For standalone backend work, use the same temporary token in both terminals. Do not persist or commit this token:

```powershell
# Terminal 1
$env:AYAYA_API_TOKEN="<temporary Base64URL token with at least 43 characters>"
conda run -n ayaya python -B -m app.server

# Terminal 2
$env:AYAYA_API_TOKEN="<the same temporary token>"
$env:AYAYA_MANAGE_BACKEND="false"
cd ui
npm run dev
```

These environment variables are development overrides:

- `AYAYA_PYTHON_EXECUTABLE`: use a specific Python executable instead of discovering the `ayaya` Conda environment.
- `AYAYA_BACKEND_CWD`: change the working directory used when Electron starts the backend.
- `AYAYA_BACKEND_BASE_URL`: connect Electron to a different loopback backend address; external mode still requires the matching `AYAYA_API_TOKEN`.

Electron discovers the `ayaya` Conda environment through Conda's JSON environment list and starts that environment's Python executable directly, so `conda run` is not kept in the long-running backend process tree.

Repository conventions for contributors and coding agents are documented in [AGENTS.md](AGENTS.md).

## Build

```powershell
cd ui
npm run build
npm run start
```

`npm run build` runs the Live2D preparation, TypeScript checks and the Vite production build. `npm start` launches the built app.

## Tests

Backend tests must use isolated test storage:

```powershell
$env:AYAYA_ENV="test"
$env:AYAYA_DATA_DIR="$env:TEMP\ayaya-test-data"
conda run -n ayaya python -B -m pytest tests -q -p no:cacheprovider
```

Test mode does not load the production `.env`, and every SQLite file, checkpoint, vector index and image must stay inside `AYAYA_DATA_DIR`.

Frontend checks:

```powershell
cd ui
npm run typecheck
npm run test:unit
```

## Local Data

A normal local run uses:

- `memory/` for backend runtime data: SQLite databases, checkpoints, images and memory stores.
- `config/chat_settings.yaml` and `config/mcp_servers.yaml` for session and MCP settings.
- `ui/user_data/` for imported Live2D models, model metadata, frontend settings and model transform data.

Credentials and generated runtime data are not committed. `.env`, the local config files, `memory/`, `agent_workspace/` and `ui/user_data/` are excluded by `.gitignore`.
