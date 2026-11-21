# Ayaya 本地 API

本文按 `main.py` 挂载的路由、`app/routes/agent.py`、`app/schemas/interaction.py` 和桌面 Electron bridge 的当前实现整理。完整 HTTP 字段约束以运行中的 `/openapi.json` 为准；事件 payload、交付时序及 Electron IPC 另见下文。

## 本地访问与通用格式

- 默认监听回环地址 `http://127.0.0.1:8000`。除 `GET /` 外，请求均须带桌面启动器提供的 `Authorization: Bearer <临时令牌>`；浏览器 Origin 请求被拒绝。
- 配置、权限及历史接口通常返回 `{"data": ..., "msg": "success", "code": 200}`。下述运行时接口直接返回 JSON 对象或 SSE，不包在 `Result.data` 中。
- `GET /` 返回存活提示；`GET /internal/ready` 和 `POST /internal/shutdown` 分别供受控桌面启动器检查就绪和关闭后端。
- `GET /health?session_id=<id>` 返回当前会话的模型名和健康状态；会话已删除时返回 404。

### HTTP 错误

| 状态码 | 触发情况 |
| --- | --- |
| 400 | 提交未知工具权限；渠道未注册、会话未订阅该渠道或试图修改桌面固定订阅；任务没有终端输出；MCP 测试或显式重连失败。 |
| 404 | 会话、MCP 服务、子 Agent、任务或历史查询终点不存在；子 Agent、任务或历史终点属于其他会话。 |
| 409 | 会话或 MCP 服务编号已存在；尝试修改 MCP 稳定编号；重连已禁用的 MCP 服务。 |
| 422 | 请求字段或查询参数无效，包括非法分页；插件参数无效时 `detail` 为字段错误列表。 |
| 500 | 保存的消息与当前消息契约不兼容，或接口遇到未预期的内部错误。 |
| 503 | 请求关闭后端，但当前进程没有由受控启动器管理。 |

普通业务错误返回 `detail` 字符串，例如查询不存在的任务：

```json
{"detail":"任务不存在"}
```

插件参数错误返回字段路径、说明和错误类型，例如记忆提取间隔填为 0：

```json
{"detail":[{"loc":["agent_plugins","memory","config","long_term_memory_every_human_messages"],"msg":"Input should be greater than or equal to 1","type":"greater_than_equal"}]}
```

请求字段校验沿用 FastAPI 的 422 格式。未授权访问仍按本地访问规则返回 401 或 403。未预期的 500 错误可能只有默认的 `Internal Server Error` 正文。

命令被拒绝时仍返回 HTTP 200 和 `accepted=false`，原因在回执的 `detail` 中。SSE 连接建立后的运行错误继续作为事件发送，内容见下文；HTTP 错误表适用于普通请求及 SSE 建立连接前的检查。

运行时取值由后端 `app/schemas/runtime_types.py` 的字符串枚举集中定义；前端 `ui/shared-types.ts` 使用同值的具名常量及联合类型。HTTP、SSE、SQLite 中仍保存既有字符串，不输出枚举成员名称。会话阶段 `SessionPhase`、轮次执行阶段 `TurnPhase`、受管进程 `TaskStatus` 和模型通知 `TaskResultStatus` 分别建模；任务停止仍从 `stopped` 映射为通知的 `cancelled`，混合终态仍为 `mixed`。Agent 内部 `done` 仍映射到交互 `run_completed`。

取值枚举与生命周期分别负责分类和执行资格：运行期间未知命令仍返回 HTTP 200 的 `accepted=false` 业务回执，未知展示事件沿既有忽略路径处理。渠道 ID 仍可动态注册，`desktop` 只是固定桌面渠道常量。两端共享 `tests/fixtures/runtime-protocol-values.json` 的固定协议样例进行取值与序列化回归检查，协议变更时须同步审核定义和样例。

## 启动与退出语义

后端生命周期 `RuntimePhase` 为内部状态，分别为 `initializing`、`running`、`stopping`、`stopped`，不进入会话快照协议。启动清理成功前和关闭期间，命令返回 `accepted=false`，不写入新的执行意图。

每次启动会删除 Inbox 中所有未交付的真人输入（包括普通、引导和排队消息），保留已交付历史和任务终态通知；通知的遗留领取重置为 `pending`。最新未完成 checkpoint 的工具调用补齐取消或未知结果，已有结果和媒体保存为历史事实，不调用模型或工具。启动清理失败会阻止服务启动，后续启动可幂等重试。

`session_snapshot` 只查询本次进程的状态，不入队桌面请求、不唤醒 Agent。同一后端进程内的事件重连不终止轮次；后端重启或应用退出会结束旧轮次，只有新真人输入才能重新激活会话。

关闭请求先封住命令、任务唤醒和桌面领取入口并结束两条 SSE，再等待 worker 和桌面持久操作退出，保存进程任务终态、可见片段与轮次终止结果。任务通知、记录和输出保留，未交付真人输入作废。已开始的副作用无法撤回；未收到可靠结果时只记录未知。

## 对话命令与会话事件

`POST /agent/commands` 接收 `ChannelCommand`，只返回受理回执，不等待 Agent 回复。请求字段为必填的 `session_id`、`origin_channel`、`kind`，以及可选的 `payload`（默认为 `{}`）、`target_run_id`、`request_id`、`task_id`。空的会话或来源被 HTTP 请求模型拒绝；未注册或未订阅的渠道由运行时返回 `accepted=false`。当前生产渠道只有 `desktop`；测试渠道只在测试中注册。桌面与将来的外部渠道共享同一 Ayaya 会话。

| `kind` | 必要字段与行为 |
| --- | --- |
| `human_turn` | `payload.message` 为文本；可附 `payload.images`（图片 data URL 数组）。文本和图片不能同时为空。输入先持久化，活动轮次存在时会引导并结束旧轮。 |
| `human_steer` | 同上，另需 `target_run_id`。匹配活动轮次时引导；目标过期时不会停止另一个轮次，可能成为排队或新输入。 |
| `queue_turn` | 同真人输入，仅在有运行中、待审批或待执行的轮次时受理；前一真人轮次正常完成后放行，可用 `cancel_queued` 撤销尚未领取的消息。 |
| `stop_turn` | 可附 `target_run_id` 限定轮次；当前轮次进入最终提交阶段后不再接受停止。 |
| `resolve_authorization` | `request_id` 指向待审批请求；`payload.kind` 为 `approve`、`reject` 或 `steer`。`steer` 另需非空 `payload.message`。两端均可提交，后端只接受第一个有效决定。 |
| `cancel_queued` | `payload.input_sequence` 为 Inbox 消息编号；仅消息来源渠道能撤销尚未领取的排队输入。 |
| `stop_background_task` | `task_id` 指向同一会话的后台任务。 |

回执包含 `status`、`accepted`、`context_id`、`run_id` 和 `detail`，例如 `{"status":"accepted","accepted":true,"context_id":"default","run_id":null,"detail":null}`。拒绝时 `accepted=false`，`detail` 说明原因，`context_id` 和 `run_id` 允许为空。未知命令仍返回 HTTP 200，例如 `{"status":"invalid","accepted":false,"context_id":"default","run_id":null,"detail":"未知运行命令"}`。`accepted=true` 只表示命令已受理，不表示输入已交付模型、工具已执行或本轮已经完成。本版没有通用 `command_id` 或自动重发／去重命令机制。

例如，桌面发送一句话：

```json
{"session_id":"default","origin_channel":"desktop","kind":"human_turn","payload":{"message":"你好","images":[]}}
```

`GET /agent/events/{session_id}?channel=desktop` 是**独立的会话 SSE 订阅**，不拥有 Agent 生命周期。每条记录是 `data: <InteractionEvent JSON>\n\n`；没有旧版的具名 `event:` 行或 `[DONE]` 哨兵。连接建立时先发送 `session_snapshot`，随后发送增量。事件流断开不停止 Agent；重连从当前快照恢复状态，不补发掉线期间全部增量或聊天历史。慢订阅者的有界队列满时会被断开。

会话展示流和桌面执行流在连续空闲 60 秒后发送注释心跳 `: ping\n\n`，不占事件序号、不进入聊天，也不逐帧写日志。心跳不会取消待取的业务事件；连接关闭时只释放订阅读取。审批的 `data.arguments` 和 `prepared_action` 必须是标准 JSON 容器，不输出 Python 内部不可变对象的字符串表示。

事件公共字段为 `session_id`、`kind`、`payload`、`origin_channel`、`run_id`、`request_id`、`task_id`、`stream_epoch`、`event_sequence`、`timestamp`；不适用的关联字段为 `null`。`stream_epoch` 在后端进程重启后改变；`event_sequence` 在同一 epoch 内按会话递增。快照沿用生成时的序号作为水位，接收端以新快照替换旧视图，只对同一 epoch 的后续增量作顺序比较。

SSE 文本分片示例（每条记录后有一个空行；以下标识仅为示例）：

```text
data: {"session_id":"default","kind":"text_chunk","payload":{"value":"你好"},"origin_channel":"desktop","run_id":"run-1","request_id":null,"task_id":null,"stream_epoch":"epoch-1","event_sequence":3,"timestamp":"2026-10-08T00:00:00+00:00"}

```

| `kind` | 主要 `payload` 与用途 |
| --- | --- |
| `session_snapshot` | `phase`、`run_id`、`origin_channel`、当前 `interrupt`、`pending_inputs`、`queued_inputs`；桌面还包含 `tasks`，不携带正在生成的正文。 |
| `human_message` | 输入事务提交后立即发布 `message`、`images`、`input_sequence`、`message_id`，不等待模型首个分片。外部渠道的真人输入也发给桌面。 |
| `queued_message` / `queued_cancelled` | 排队消息的 `input_sequence`，入队事件还含 `message`、`images`。 |
| `run_started` / `run_completed` | 轮次开始／结束；恢复开始时有 `resumed=true`，结束事件的 `payload.value` 为 Agent 完成值或打断标记。 |
| `text_chunk` / `tool_call` | Agent 事件包在 `payload.value`，分别为文本片段、工具名。 |
| `interrupt` | `payload.value` 是中断展示对象；审批的 `type=authorization_request`，屏幕执行的 `type=continuation_request`。顶层 `request_id` 对应该中断。 |
| `authorization_resolved` | `payload.outcome` 为 `approve`、`reject`、`steer` 或 `cancelled`，`actor_channel` 表示处理入口；顶层 `request_id` 指定应关闭的审批。 |
| `task_status` | `task_id`、`status`、`exit_code`；任务状态及自动唤醒回复暂只投递桌面。 |
| `error` | `payload.detail` 或 Agent 错误值。 |

普通轮次事件投递给桌面和该轮的来源渠道，不转给其他外部渠道；后台唤醒只送桌面。`authorization_resolved` 仍按原审批请求来源投递，即使决定由另一入口提交。审批的决定经命令接口提交，其他端由事件更新展示。历史消息仍须通过历史接口查询；事件快照不是完整聊天记录。

## 桌面执行器与后台任务

子 Agent 查询使用以下接口；查询只读取记录：

| 接口 | 响应及错误 |
| --- | --- |
| `GET /agent/subagents/{session_id}` | 返回 `{"subagents":[...]}`；没有子 Agent 时返回空列表。 |
| `GET /agent/subagents/{session_id}/{subagent_id}?interaction_limit=10` | 返回定义、最近交互、结果及当前状态；子 Agent 不存在或属于其他会话时返回 404。`interaction_limit` 至少为 1，无效时返回 422。 |

屏幕工具由 **Electron 主进程**执行，不由聊天 Renderer 处理。它持续订阅 `GET /desktop-executor/requests`，这是一条与会话展示事件分开的 SSE；每条 `data:` JSON 包含 `session_id`、`request_id`、`kind`（`capture_screen` 或 `perform_screen_action`）和 `payload`。连接时先给出仍待领取的请求，断线后可重连。

1. `POST /desktop-executor/claim`，请求 `{"session_id":"...","request_id":"..."}`，返回 `{"claimed":true|false}`。后端复核当前 continuation、工具授权和是否已领取；必须在截屏或点击之前调用。
2. 领取成功后，主进程执行操作。`POST /desktop-executor/result` 请求 `{"session_id":"...","request_id":"...","result":{...}}`，返回 `{"accepted":true|false}`。截屏结果含 `screenshot_data`（data URL）、`width`、`height`；屏幕动作结果含 `executed`，成功时还可附操作后截图，失败时含 `error`。结果回传暂时失败可重试**同一结果**，不能重做屏幕动作。

后端重启会终止所有旧轮次：旧审批与未领取请求作废；已领取但无结果的动作记录为“结果未知”；已有桌面回报作为工具结果事实保存。原调用及同批后续工具均不自动续跑。旧 request_id 的领取、审批和结果回传不再有效。工具最终结果仍归 Agent 工具协议，交互事件只承载可见状态。

| 接口 | 请求 / 响应 |
| --- | --- |
| `GET /tasks/{session_id}/records` | 返回 `{"tasks":[...]}`；仅列显式后台或前台超时后移交的任务。每条包含 `task_id`、会话、命令、`status`、`background=true`、PID、退出码、输出路径、时间、来源运行／工具调用标识、`output_ref` 和 `recent_output`。状态为 `running`、`completed`、`failed`、`stopped` 或 `orphaned`。前台记录仍保留完整输出并可按 task_id 查询；旧任务记录缺少模式时保持可见。 |
| `GET /tasks/{session_id}/{task_id}/output?offset=0&limit=16384` | 按字节偏移分页返回 `{"text":"...","next_offset":123,"has_more":false}`。`offset` 不小于 0，`limit` 范围为 1–65536；任务必须属于路径中的会话。 |

任务不存在或属于其他会话时，输出接口返回 404；任务没有终端输出时返回 400；分页参数无效时返回 422。任务列表为空时仍返回成功。

停止任务通过 `POST /agent/commands` 的 `stop_background_task` 命令。`run_ps` 的前台、显式后台及超时转后台使用同一任务记录和追加式输出文件；任务首次进入终态时，记录变更和一条 Agent Inbox 通知在同一 SQLite 事务中提交。任务通知只允许唤醒本次启动后已由新真人输入激活的会话；未激活会话的通知留在 Inbox，等新真人输入到达后一起交付。重启后遗留的 `running` 记录显示为 `orphaned`，其通知同样不能单独启动 Agent。

`run_ps` 直接启动非交互 `powershell.exe`，继承后端进程的环境变量，并以仓库的 `agent_workspace/` 文件夹为工作目录；不自动激活同名 Conda 环境。程序已经输出并刷新的内容持续保存到任务日志，运行期间即可分页读取。

## 渠道订阅与桌面 bridge

`GET /agent/channels/{session_id}` 返回 `{"channels":[{"channel":"desktop","display_name":"桌面","enabled":true,"ready":true}]}`；`PUT /agent/channels/{session_id}/{channel}` 请求 `{"enabled":true|false}`，返回更新后的同形列表。订阅设置按会话持久化；桌面固定启用、不可修改。当前设置页仅能显示真实注册的桌面渠道；没有真实 QQ Adapter。

修改未注册渠道或桌面固定订阅时返回 400。会话 SSE 建立连接前也会检查渠道；渠道未注册或该会话未订阅时返回 400。

聊天 Renderer 不直接建立上述 HTTP/SSE 连接。Electron preload 向 Renderer 暴露 `submitInteractionCommand()`、`subscribeDesktopSession()`、`onInteractionEvent()`、`getChannelSubscriptions()`、`setChannelSubscription()`、`getBackgroundTasks()` 和 `getBackgroundTaskOutput()`；其类型在 `ui/shared-types.ts` 的 `DesktopPetApi` 中。主进程处理对应 IPC，把会话 SSE 事件通过 `desktop-pet:interaction-event` 转给 Renderer，并独立持有屏幕执行订阅。桌面提交命令时固定 `origin_channel=desktop`。

新聊天页面通过 `subscribeDesktopSession(sessionId, { refreshSnapshot: true })` 重新建立展示连接；普通发送省略该选项以复用连接。页面先注册事件监听，应用首次快照并结束历史查询后才允许发送或排队；历史查询失败会显示错误，但仍应用快照中的当前运行状态。主进程只转发当前订阅代次的事件，旧连接的迟到回调不会进入新页面。

任务快捷入口位于聊天记录标题栏，数量只计运行中的后台任务；全部结束后四秒通知消失并隐藏标题栏。`openSettingsWindow("tasks")` 直接进入已有设置窗口的任务页，新窗口通过页面查询参数初始化，已有窗口通过 `onOpenSettingsPage()` 导航。设置窗口复用桌面事件转发，并仅在任务页可见且有运行任务时每两秒刷新耗时和最近输出；关闭或切页后停止刷新。真实断线、重连和结果回传失败进入统一前端日志，同一断线期间连续重试不重复刷日志。

回复的表情标签由 Renderer 在展示前解析；聊天列表和桌面气泡共用清洗后的正文，标签跨分片未闭合时暂不显示。标签动作在同一 run 内去重，读取历史不重播动作。后端事件、checkpoint 和协议历史仍保留原始正文。

桌面页面初始化、会话切换和重连时收到快照后读取聊天历史；重连时仍在运行或等待中的回答不恢复半段正文，也不追加其后续文字，直到完成后统一显示已保存内容。历史查询期间已收到开始或文字事件的回答同样延后展示。普通连接的新回答仍实时显示。

历史还在等待人工审批、回答完成（含用户停止或引导打断）、运行错误时刷新；同审批编号成功刷新一次。自动桌面操作、审批解除、文字分片、排队和任务状态更新不刷新历史，停止命令回执也不触发查询。错误仅恢复此前已保存内容，不保证最后片段已保存。列表和桌面气泡复用同一次历史查询，读取失败保留已有显示并报告错误，之后由下一次刷新触发点重试，不增加定时查询。

## 模型配置与工具权限

| 接口 | 作用 |
| --- | --- |
| `POST /chat_settings`、`PUT /chat_settings` | 新增或更新完整模型配置；均返回规范化后的配置。 |
| `GET /chat_settings/{session_id}`、`DELETE /chat_settings/{session_id}` | 读取或删除指定会话的模型配置。 |
| `GET /tools`、`GET /plugins` | 列出可用本地工具及插件元数据。 |
| `GET /tool-permissions/{session_id}` | 列出本地和 MCP 工具的稳定键、策略和可用状态。 |
| `PUT /tool-permissions/{session_id}` | 请求体 `{"tool_permissions":{"local:screenshot":"ask","mcp:server:tool":"deny"}}`；一次保存当前会话的完整策略映射。 |

ChatSettings 包含 `session_id`、`model_name`、`openai_api_key`、`openai_base_url`、`temperature`、`system_prompt`、`context_strategy`，以及 `tool_permissions`、`tool_permissions_schema_version`、`agent_plugins`、`mcp.servers` 和可选角色资料。现行工具策略为稳定键映射，取值仅 `allow`、`ask`、`deny`；`mcp.servers[server_id]` 只保存 `enabled` 和可选 `identity_fingerprint`。请以 `/openapi.json` 中的 `ChatSettings` schema 获取全部约束，并参考 `config/chat_settings.sample.yaml` 的可运行示例。

新增会话编号已存在时返回 409；读取、更新、删除设置以及读写工具权限时，会话已删除则返回 404。提交未知工具权限返回 400，整批权限不保存。新增或更新配置时，插件参数无效返回 422，原配置保持不变。`GET /plugins/mcp-status/{session_id}` 查询插件连接状态，会话不存在时也返回 404。

## MCP Server 管理

所有接口位于 `/mcp`：

| 接口 | 作用 |
| --- | --- |
| `GET /mcp/servers`、`POST /mcp/servers` | 列表和新增 Server。 |
| `POST /mcp/servers/test` | 测试 Server 配置但不保存。 |
| `PUT /mcp/servers/{server_id}`、`DELETE /mcp/servers/{server_id}` | 更新或删除 Server；删除时清理模型引用和相应权限。 |
| `POST /mcp/servers/{server_id}/reconnect` | 重新连接指定 Server。 |
| `GET /mcp/servers/{server_id}/tools` | 读取当前服务的工具目录。 |

服务配置使用 `transport=stdio`（`command`、`args`、`cwd`、`env`）或 `transport=streamable_http`（`url`、`headers`）；二者共有 `id`、`name`、`enabled`、连接和调用超时字段。启用服务不等于授予逐工具权限，应使用稳定键权限接口。

新增编号重复或更新时试图修改编号，返回 409；更新、删除、重连和工具查询找不到服务时返回 404。测试连接或显式重连失败返回 400，重连已禁用的服务返回 409。新增和更新先保存配置，再尝试连接；连接失败仍返回 200，响应中的 `runtime.status` 为 `error`，`last_error` 说明原因。

## 历史与图片

- `GET /chat_history/{session_id}?start=0&limit=200`：在可见消息投影后分页；`start` 不小于 0，`limit` 不小于 1。
- `GET /chat_history_last_n/{session_id}?n=100`：读取最后 N 条可见消息，按时间升序返回；`n` 范围为 1–500。可加 `through_message_id` 限定读取到某条消息；该消息不存在或属于其他会话时返回 404。
- 两者均返回 `Result.data` 数组，元素为 `{id,role,content,timestamp,images,origin_channel}`。`role` 可能是 `Human`、`AI` 或 `AI_Tool_Calling`；真人和 Assistant 的来源由 `origin_channel` 标识，旧记录按 `desktop` 解释。**工具产生的图片也可投影为带 `images` 的 `AI` 消息**，并非只有 `Human` 才有图片。
- `GET /images/{relative_path}` 读取授权范围内的已保存图片；历史 `images` 字段是本地相对路径，请交给桌面端图片 URL 解析逻辑处理，不要假设它始终是根目录文件名。

原始工具消息和旧记录不等同于历史 UI 投影；如果协议行与当前消息契约不兼容，历史接口返回 HTTP 500，而不会静默回退到旧表。

未指定查询终点且没有历史时返回成功的空数组；分页参数无效时返回 422。
