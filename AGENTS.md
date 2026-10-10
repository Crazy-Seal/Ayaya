# 仓库规范

## 项目结构与模块组织

本项目是单用户、本地独立运行的应用。架构与功能决策应面向本地桌面使用；除非用户明确要求，否则不得假设存在多用户、分布式或远程部署需求。

FastAPI 后端入口是 `main.py`。应用代码位于 `app/`：HTTP 接口在 `routes/`，请求与响应模型在 `schemas/`，持久化逻辑在 `crud/`，编排逻辑在 `services/`，智能体、工具、插件与记忆的实现在 `agent/`。后端测试位于 `tests/`。Electron/Vite 前端位于 `ui/`：渲染进程代码在 `ui/src/`，Electron 主进程代码在 `ui/electron/`，Live2D 静态资源在 `ui/public/`。运行时状态应存放在 `memory/`，不得纳入版本控制。配置示例位于 `config/` 和 `.env.sample`。

## 构建、测试与开发命令

在 `ayaya` Conda 环境中运行后端：

```powershell
conda run -n ayaya pip install -r requirements.txt
conda run -n ayaya uvicorn main:app --reload --reload-exclude "agent_workspace/*"
conda run -n ayaya python -B -m pytest tests -q -p no:cacheprovider
```

桌面 UI：在 `ui/` 目录中执行 `npm install`，使用 `npm run dev` 启动 Vite 与 Electron 开发环境，使用 `npm run build` 生成生产构建并执行 TypeScript 检查，使用 `npm run start` 启动构建后的应用。

## 代码风格与命名规范

Python 使用四个空格缩进，TypeScript 使用两个空格缩进，并与周围代码保持一致。Python 模块与函数优先使用 `snake_case`，类与 TypeScript 类型使用 `PascalCase`，前端模块文件使用 `kebab-case.ts`。TypeScript 必须兼容 `strict: true`。模块职责应保持集中，并维护路由、服务与 DAO 之间的边界。控制台输出与日志消息在可行时使用中文。

### 注释要求

注释的目标是让读者沿着项目实际功能理解代码，并能把说明逐项对应到执行位置，无需再请人解释注释中的术语。

- 所有代码注释使用中文；Google 风格的章节名（如 `Args:`、`Returns:`、`Yields:`、`Raises:`、`Attributes:`）及代码标识符保留英文。
- 所有类、函数、方法都必须有 Google 风格的文档注释，包括私有成员、构造方法、回调和测试中的定义。Python 使用定义内的 docstring；TypeScript/JavaScript 使用紧邻定义之前的 `/** ... */` 文档注释，并采用相同的 Google 风格组织说明。
- 文档注释先用中文概述职责，必要时说明调用约束和副作用；按实际情况补充参数、返回值、生成值、异常和属性等章节。参数或属性说明包含名称、类型和含义；返回值、生成值和异常说明包含类型和含义或触发条件。
- 使用贴近项目功能的语言，例如“用户发送消息”“等待审批”“后台任务结束”“通知界面显示”。专业术语应说明具体指代的对象和行为；使用“槽位”“冻结”“仲裁”“视图”等词时，不能让读者自行猜测其项目含义。
- 写清对象和职责：谁调用、何时调用、处理什么数据、交给谁继续处理。回调尤其要说明调用方、触发时机以及调用前已经完成的操作；等待逻辑要说明等待什么，以及由谁通知结束等待。
- 模块、类、函数、方法内部的复杂逻辑必须有足够的注释密度。在关键分支、状态变化、数据库读写、并发控制、异步等待、异常和清理的对应位置逐步解释，不能仅在方法开头概括后留下大段未说明的复杂代码。
- 内部注释应解释触发条件、处理目的、执行顺序和约束。例如说明为什么必须先保存新消息，再请求停止旧回答，以及保存失败时如何处理。注释提供理解所需的信息，不机械复述赋值或函数调用，也不重复堆叠笼统说明。
- 明确数据所在位置和处理阶段：区分内存缓存、Inbox 记录、Agent 消息和 checkpoint，区分“已受理”“已领取”“已交付”“已执行”。只描述当前代码实际保证的行为，例如加入内存状态不等于已经持久保存。
- 复杂逻辑的说明注释使用标号理清逻辑：主步骤使用 `1.`、`2.`、`3.`，子步骤使用 `1.1.`、`1.2.`，更深层级按需继续细分。编号在每个独立逻辑块内从 `1.` 开始，与实际代码的执行顺序和分支位置对应；注释放在对应代码之前，Python 使用 `#`，TypeScript/JavaScript 使用 `//`。
- 代码结构应支持按注释阅读：按功能分段，使用明确名称；复杂流程必要时拆成职责清楚的方法，让核心循环和回调的调用关系容易定位。整理结构和注释时保持既有行为，行为修改按任务要求单独验证。
- 新增或修改代码时同步补齐对应定义的文档注释及复杂逻辑的编号注释。修改分支、提取方法或改变时序后，同步调整说明和编号，确保说明与实际代码一致。

验收标准：读完注释，能够回答“谁在什么情况下，按什么顺序，处理了什么；为什么这样处理；失败后会怎样”，并能逐项定位到代码。

复杂逻辑的编号注释示例：

```python
# 1. 校验页码，避免接受零或负数页码
# 1.1. 页码无效时抛出参数错误，终止后续查询
# 1.2. 页码有效，继续查询流程
# 2. 以当前认证用户为范围查询订单
# 3. 转换状态与金额快照，返回分页结果
```

成员和回调说明示例：

```python
"""协调当前会话的输入处理。

Attributes:
    _wakeups (dict[str, asyncio.Event]): 新消息、审批决定或任务结果到达时，让对应会话的 worker 结束等待，重新检查需要处理的数据。
"""

async def publish_committed_human_message(message: HumanMessage) -> None:
    """Agent 保存真人消息成功后，通知界面显示，不等待模型生成第一段回答。

    本方法由 Agent 的 on_input_committed 回调调用；调用前 checkpoint 和
    Inbox 的 delivered 标记已在同一事务中保存。

    Args:
        message (HumanMessage): 已保存的真人消息，提供正文、来源和消息编号。
    """
    # 1. 使用已保存消息的编号发送显示事件，让前端能够与聊天历史去重。
    await publish_human_message_event(message)
```

## 测试规范

后端使用 Pytest 测试框架。测试文件命名为 `test_<feature>.py`，测试函数命名为 `test_<behavior>`。行为变更必须补充回归测试；当前未配置数值化的覆盖率门槛。`tests/conftest.py` 会在导入后端模块前创建临时数据目录。测试绝不能访问、修改或删除生产存储。

## 环境与安全

未设置 `AYAYA_ENV` 时默认为生产环境，使用仓库中的 `memory/`、`config/chat_settings.yaml` 和根目录的 `.env`。显式运行生产环境时，设置 `AYAYA_ENV=production` 并移除 `AYAYA_DATA_DIR` 环境变量。测试时，设置 `AYAYA_ENV=test`，并将 `AYAYA_DATA_DIR` 指向生产 `memory/` 之外的目录；所有 SQLite、检查点、Chroma、Mem0/Qdrant、图片与会话配置都必须存放在该测试目录中。测试模式不加载生产 `.env`。手动启动测试后端前，先创建 `$env:AYAYA_DATA_DIR\config\chat_settings.yaml`。不得提交凭据或生成的运行时数据。

## 提交与拉取请求规范

除非用户在当前请求中明确授权相应操作，否则不得暂存文件、创建或修改提交、重置、变基、压缩提交、推送，或以其他方式改变 Git 状态或历史。要求实现、修复、测试或遵循某个工作流，本身不代表授权提交。未获授权时，保留未提交的改动，并报告工作区状态。

所有提交消息必须遵循 Conventional Commits，格式为 `<type>(<scope>): <description>`，其中作用域可省略。每次提交对应一个逻辑变更，并使用简洁的中文祈使句描述，例如 `fix(agent): 修复流式工具调用聚合`。常用类型包括 `feat`、`fix`、`refactor`、`test`、`docs`、`build`、`ci` 和 `chore`。拉取请求应说明目的、实现影响、测试命令与结果、配置变更及关联的问题。涉及可见 UI 变更时，应附截图或简短录屏。

## 智能体技能

### 问题跟踪

问题与规格说明以本地 Markdown 文件的形式在 `.scratch/` 下跟踪。参见 `docs/agents/issue-tracker.md`。

### 分诊标签

分诊使用五个默认标准标签。参见 `docs/agents/triage-labels.md`。

### 领域文档

本仓库采用单一上下文的领域文档布局。参见 `docs/agents/domain.md`。
