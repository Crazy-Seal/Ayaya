# Ayaya 本地 API

本文按 `main.py` 挂载的路由、`app/routes/` 的请求模型和当前 SSE 格式整理。完整字段约束以运行中的 `/openapi.json` 为准；桌面端通过 Electron 主进程调用这些本地接口。

## 本地访问与通用格式

- 默认监听回环地址 `http://127.0.0.1:8000`。除 `GET /` 外，请求均须带桌面启动器提供的 `Authorization: Bearer <临时令牌>`；浏览器 Origin 请求被拒绝。
- 普通接口返回 `{"data": ..., "msg": "success", "code": 200}`。校验失败返回 HTTP 422；其他失败按对应路由返回 HTTP 错误及 `detail`。
- `GET /` 返回存活提示；`GET /internal/ready` 和 `POST /internal/shutdown` 分别供受控桌面启动器检查就绪和关闭后端。
- `GET /health?session_id=<id>` 返回当前会话的模型名和健康状态。

## 聊天、中断与继续执行

| 接口 | 请求 / 响应 |
| --- | --- |
| `POST /chat` | JSON：`{"session_id":"...","message":"你好","images":["data:image/png;base64,..."]}`。`message` 必填且非空，`session_id` 默认为 `default`，`images` 可省略。返回 SSE。 |
| `GET /agent/pending-interrupt/{session_id}` | 返回 `data.pending=false`；若有待处理请求则返回 `data.pending=true` 与 `data.interrupt.value`，供桌面端重启后恢复。 |
| `POST /tool-authorization/respond` | JSON：`session_id`、`request_id`、`kind`（`approve`、`reject`、`steer`）；`steer` 还必须有非空 `message`。返回新的 SSE；`steer` 启动新用户输入，不继续执行已中断工具。 |
| `POST /tool-continuation/claim` | JSON：`session_id`、`request_id`。前端产生截屏/屏幕操作副作用**之前**先认领并复核；已认领或授权失效返回 409。成功返回 `data.claimed=true`。 |
| `POST /tool-continuation/respond` | JSON：`session_id`、`request_id`、`response` 对象。前端提供实际截屏或操作结果，返回新的 SSE。 |

授权请求通过 `event: interrupt` 发送，`value.type` 为 `authorization_request`；`value.data` 包含 `source`、`tool_identity`、`tool_name`、`arguments`，按需包含 `prepared_action`。用户作出决定后调用统一授权接口。授权通过的工具可再产生 `continuation_request`：`value.data.kind` 为 `capture_screen` 或 `perform_screen_action`，前端须先 `claim`，再截屏/操作并 `respond`。`capture_screen` 的响应可包含 `screenshot_data`（图片 data URL）、`width`、`height`；`perform_screen_action` 的响应可包含 `executed`、`error` 和操作后的截图信息。

SSE 示例（每个事件之间有空行）：

```text
data: {"response":"正在处理"}

event: tool_call
data: {"tool_name":"screenshot"}

event: interrupt
data: {"value":{"type":"continuation_request","request_id":"request-1","message":"正在截取屏幕。","data":{"kind":"capture_screen","payload":{}}}}

```

收到中断时，前端将 `interrupt` 视为当前段的终态，停止读取并通过响应接口开启新流；后端路由在流结束时仍可能写出 `data: [DONE]`。无中断的正常结束以 `data: [DONE]` 标识；流中出错发送 `event: error` 和 `{"detail":"..."}`。`request_id` 是持久化中断标识，不是前端每段流使用的关联 ID。响应前检查同一会话的待处理请求；错误或过期中断不能作为已执行成功处理。

## 模型配置与工具权限

| 接口 | 作用 |
| --- | --- |
| `POST /chat_settings`、`PUT /chat_settings` | 新增或更新完整模型配置；均返回规范化后的配置。 |
| `GET /chat_settings/{session_id}`、`DELETE /chat_settings/{session_id}` | 读取或删除指定会话的模型配置。 |
| `GET /tools`、`GET /plugins` | 列出可用本地工具及插件元数据。 |
| `GET /tool-permissions/{session_id}` | 列出本地和 MCP 工具的稳定键、策略和可用状态。 |
| `PUT /tool-permissions/{session_id}` | 请求体 `{"tool_permissions":{"local:screenshot":"ask","mcp:server:tool":"deny"}}`；一次保存当前会话的完整策略映射。 |

ChatSettings 包含 `session_id`、`model_name`、`openai_api_key`、`openai_base_url`、`temperature`、`system_prompt`、`context_strategy`，以及 `tool_permissions`、`tool_permissions_schema_version`、`agent_plugins`、`skills`、`mcp.servers` 和可选角色资料。现行工具策略为稳定键映射，取值仅 `allow`、`ask`、`deny`；`mcp.servers[server_id]` 只保存 `enabled` 和可选 `identity_fingerprint`。请以 `/openapi.json` 中的 `ChatSettings` schema 获取全部约束，并参考 `config/chat_settings.sample.yaml` 的可运行示例。

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

## 历史与图片

- `GET /chat_history/{session_id}?start=0&limit=200`：在可见消息投影后分页；`start` 不小于 0，`limit` 不小于 1。
- `GET /chat_history_last_n/{session_id}?n=100`：读取最后 N 条可见消息，按时间升序返回；`n` 范围为 1–500。
- 两者均返回 `Result.data` 数组，元素为 `{id,role,content,timestamp,images}`。`role` 可能是 `Human`、`AI` 或 `AI_Tool_Calling`；**工具产生的图片也可投影为带 `images` 的 `AI` 消息**，并非只有 `Human` 才有图片。
- `GET /images/{relative_path}` 读取授权范围内的已保存图片；历史 `images` 字段是本地相对路径，请交给桌面端图片 URL 解析逻辑处理，不要假设它始终是根目录文件名。

原始工具消息和旧记录不等同于历史 UI 投影；如果协议行与当前消息契约不兼容，历史接口返回 HTTP 500，而不会静默回退到旧表。
