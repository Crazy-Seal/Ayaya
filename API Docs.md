# 前后端接口文档

## 通用返回结构
所有成功响应统一为 `Result`（**`/chat` 除外，`/chat` 使用流式 `text/event-stream`**）：

```json
{
  "data": {},
  "msg": "success",
  "code": 200
}
```

说明：
- `data`：业务数据，可能是对象或 `null`
- `msg`：结果消息
- `code`：业务状态码（当前成功为 `200`）

错误响应由 `HTTPException` 返回，结构示例：

```json
{
  "detail": "session_id not found: xxx"
}
```

## 1.LLM配置

### 增：POST /chat_settings

#### 接收参数：

- `settings`：模型配置（body, json）

```json
{
  "session_id": "test-session-123",
  "model_name": "模型名称",
  "openai_api_key": "API密钥",
  "openai_base_url": "API基础URL",
  "temperature": 0.7,
  "system_prompt": "系统提示词",
  "tools_list": ["tool1", "tool2"]
}
```

#### 返回结果：
成功：

```json
{
  "data": null,
  "msg": "success",
  "code": 200
}
```

失败（session_id重复）：

```json
{
  "detail": "session_id already exists: xxx"
}
```

### 删：DELETE /chat_settings/{session_id}

#### 接收参数：

- `session_id`：会话ID（path, str）

#### 返回结果：
成功：

```json
{
  "data": null,
  "msg": "success",
  "code": 200
}
```

失败（session_id不存在）：

```json
{
  "detail": "session_id not found: xxx"
}
```

### 查：GET /chat_settings/{session_id}

#### 接收参数：

- `session_id`：会话ID（path, str）

#### 返回结果：
成功：

```json
{
  "data": {
      "session_id": "test-session-123",
      "model_name": "模型名称",
      "openai_api_key": "API密钥",
      "openai_base_url": "API基础URL",
      "temperature": 0.7,
      "system_prompt": "系统提示词",
      "tools_list": ["tool1", "tool2"]
  },
  "msg": "success",
  "code": 200
}
```

失败（session_id不存在）：

```json
{
  "detail": "session_id not found: xxx"
}
```

### 改：PUT /chat_settings

#### 接收参数：

- `settings`：模型配置（body, json）

```json
{
  "session_id": "test-session-123",
  "model_name": "模型名称",
  "openai_api_key": "API密钥",
  "openai_base_url": "API基础URL",
  "temperature": 0.7,
  "system_prompt": "系统提示词",
  "tools_list": ["tool1", "tool2"]
}
```

#### 返回结果：
成功：

```json
{
  "data": null,
  "msg": "success",
  "code": 200
}
```

失败（session_id不存在）：

```json
{
  "detail": "session_id not found: xxx"
}
```

## 2.聊天接口

### POST /chat

#### 接收参数：

- `payload`（body, json）

```json
{
  "message": "你好",
  "session_id": "a9ea0407-6a54-4535-b424-b7cd454d7bcd",
  "images": ["data:image/png;base64,iVBORw0KGgoAAA...", "data:image/png;base64,iVBORw0KGgoBBB..."]
}
```

字段说明：
- `message`：用户输入文本（必填）
- `session_id`：会话ID（必填）
- `images`：图片列表（可选，`list[string] | null`，传入 data URL 格式的 base64 字符串数组）


#### 返回结果：

- `Content-Type`: `text/event-stream`
- 返回为 SSE 流，每个数据片段格式如下：

```text
data: {"response":"你好"}

data: {"response":"，有什么我可以帮助你的？"}

data: [DONE]
```

说明：
- 每个 `data:` 事件都包含本次增量文本分片
- 当收到 `data: [DONE]` 时表示本轮回答结束
- 前端应按顺序拼接 `response` 字段得到完整回复

失败（模型调用异常）可能返回错误事件：

```text
event: error
data: {"detail":"OPENAI_API_KEY is not set. Please configure your API key."}
```
## 3.聊天记录查询

### 图片存储说明

用户发送的图片会被保存到后端服务器，数据库返回的 `images` 字段为**纯文件名列表**：

```
"images": ["2026-05-16_14-30-00_a1b2c3d4.png", "2026-05-16_14-30-01_e5f6g7h8.png"]
```

**文件命名格式**：`{日期}_{时间}_{UUID}.png`，例如 `2026-05-16_14-30-00_a1b2c3d4.png`

**前端访问图片**：拼接后端地址 + `/images/` + 文件名

```
http://127.0.0.1:8000/images/2026-05-16_14-30-00_a1b2c3d4.png
```

**后端存储位置**：项目根目录下的 `memory/screenshot/` 文件夹

### GET /chat_history/{session_id}

#### 接收参数：

- `session_id`：会话ID（path, str）
- `start`：起始位置（query, int, 默认 `0`，且 `>= 0`）
- `limit`：查询条数（query, int, 默认 `200`，且 `>= 1`）

#### 返回结果：
成功：

```json
{
  "data": [
    {
      "role": "Human",
      "content": "你好",
      "timestamp": "2026-03-23T10:00:00+08:00",
      "images": null
    },
    {
      "role": "Human",
      "content": "看看这些图片",
      "timestamp": "2026-03-23T10:05:00+08:00",
      "images": ["2026-03-23_10-05-00_a1b2c3d4.png", "2026-03-23_10-05-01_e5f6g7h8.png"]
    },
    {
      "role": "AI",
      "content": "你好，有什么我可以帮你？",
      "timestamp": "2026-03-23T10:00:02+08:00",
      "images": null
    }
  ],
  "msg": "success",
  "code": 200
}
```

说明：
- `data` 为聊天记录数组，按时间升序返回
- 分页语义：从第 `start` 条开始，返回最多 `limit` 条（start从0开始计数）
- `timestamp` 为服务端转换后的本地时区时间（ISO 8601）
- `images` 为图片文件名列表（纯文件名，非完整路径），仅 Human 消息可能有值，AI 消息为 `null`
- 当该会话暂无记录或超出范围时，`data` 返回空数组 `[]`

### GET /chat_history_last_n/{session_id}

#### 接收参数：

- `session_id`：会话ID（path, str）
- `n`：查询条数（query, int, 默认 `100`，且 `1 <= n <= 500`）

#### 返回结果：
成功：

```json
{
  "data": [
    {
      "role": "Human",
      "content": "你好",
      "timestamp": "2026-03-23T10:00:00+08:00",
      "images": null
    },
    {
      "role": "AI",
      "content": "你好，有什么我可以帮你？",
      "timestamp": "2026-03-23T10:00:02+08:00",
      "images": null
    }
  ],
  "msg": "success",
  "code": 200
}
```

说明：
- `data` 为聊天记录数组，返回该会话最后 `n` 条记录，按时间升序排列
- `timestamp` 为服务端转换后的本地时区时间（ISO 8601）
- `images` 为图片文件路径列表，仅 Human 消息可能有值，AI 消息为 `null`
- 当该会话暂无记录时，`data` 返回空数组 `[]`
- 适用于需要快速获取最近聊天记录的场景，无需分页

---

## 5.截屏确认接口

### 概述

当 Agent 调用截屏工具时，后端会暂停执行并请求用户确认。前端需要：

1. 监听 `/chat` SSE 流中的 `interrupt` 事件
2. 显示确认对话框让用户选择"允许"或"拒绝"
3. 调用 `POST /screenshot/respond` 返回用户决定
4. 继续处理 SSE 流式响应

### SSE 事件：interrupt

当 Agent 请求截屏时，`/chat` 接口会给前端发送 `interrupt` 事件：

```text
event: interrupt
data: {"value": {"type": "screenshot_request", "request_id": "550e8400-e29b-41d4-a716-446655440000", "message": "Agent 请求截取屏幕，是否允许？"}}
```

#### 事件字段说明

数据外层为 `value` 包装，内部包含实际中断信息：

| 字段 | 类型 | 说明 |
|------|------|------|
| `value` | `object` | 包装对象，包含实际的中断请求数据 |
| `value.type` | `string` | 固定值 `"screenshot_request"`，表示截屏请求类型 |
| `value.request_id` | `string` | 请求唯一标识（UUID格式），可用于前端日志追踪 |
| `value.message` | `string` | 展示给用户的提示文本，可直接显示在确认对话框中 |

#### 前端处理流程

```
1. 收到 event: interrupt
2. 暂停当前 SSE 连接（不要关闭，后续会有响应）
3. 解析 data 中的 JSON
4. 显示确认对话框，展示 message 内容
5. 用户点击"允许"或"拒绝"
6. 调用 POST /screenshot/respond
7. 继续处理后续 SSE 事件（新建立的 SSE 连接）
```

**注意**：收到 `interrupt` 事件后，**原 SSE 流会终止**（后端发送 interrupt 后立即 return）。前端需要先让用户确认，然后调用 `/screenshot/respond` 接口，该接口会返回新的 SSE 流。

---

### POST /screenshot/respond

用户确认后恢复对话执行。

#### 接收参数

- `payload`（body, json）

```json
{
  "session_id": "a9ea0407-6a54-4535-b424-b7cd454d7bcd",
  "approved": true
}
```

#### 字段说明

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `session_id` | `string` | 是 | 会话ID，需与之前 `/chat` 请求的 session_id 一致 |
| `approved` | `boolean` | 是 | 用户决定：`true` = 允许截屏，`false` = 拒绝截屏 |

#### 返回结果

- `Content-Type`: `text/event-stream`
- 返回 SSE 流，格式与 `/chat` 完全一致：

**用户允许截屏时**：

```text
data: {"response":"好的，我已经获取到屏幕截图了"}

data: {"response":"，让我看看..."}

data: [DONE]
```

**用户拒绝截屏时**：

```text
data: {"response":"看起来你不想让我截屏"}

data: {"response":"，没关系，有其他需要帮助的吗？"}

data: [DONE]
```

#### 错误响应

**会话已过期**（会话不存在或已被清理）：

```text
data: {"response":"[系统]会话已过期"}

data: [DONE]
```

**恢复对话失败**：

```text
event: error
data: {"detail":"恢复对话失败: ..."}
```

---

### 完整交互示例

#### 场景：用户允许截屏

```
┌─────────────────────────────────────────────────────────────────────┐
│ 前端                                                                 │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  1. POST /chat                                                       │
│     { "message": "帮我看看屏幕上有什么", "session_id": "xxx" }        │
│                                                                      │
│  2. SSE 响应开始...                                                  │
│     data: {"response":"好的，让我截个屏看看"}                         │
│                                                                      │
│  3. 收到 interrupt 事件：                                            │
│     event: interrupt                                                 │
│     data: {"type":"screenshot_request",...,"message":"Agent请求..."} │
│                                                                      │
│  4. 显示确认对话框：                                                  │
│     ┌────────────────────────────────────┐                          │
│     │  Agent 请求截取屏幕，是否允许？     │                          │
│     │                                    │                          │
│     │    [拒绝]         [允许]           │                          │
│     └────────────────────────────────────┘                          │
│                                                                      │
│  5. 用户点击 [允许]                                                   │
│                                                                      │
│  6. POST /screenshot/respond                                         │
│     { "session_id": "xxx", "approved": true }                       │
│                                                                      │
│  7. SSE 响应：                                                       │
│     data: {"response":"我看到屏幕上有..."}                            │
│     data: [DONE]                                                     │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

#### 场景：用户拒绝截屏

```
┌─────────────────────────────────────────────────────────────────────┐
│ 前端                                                                 │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  1-4. (同上)                                                         │
│                                                                      │
│  5. 用户点击 [拒绝]                                                   │
│                                                                      │
│  6. POST /screenshot/respond                                         │
│     { "session_id": "xxx", "approved": false }                      │
│                                                                      │
│  7. SSE 响应：                                                       │
│     data: {"response":"好的，您拒绝了截屏"}                             │
│     data: {"response":"，有其他我可以帮你的吗？"}                      │
│     data: [DONE]                                                     │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

---

### 注意事项

1. **Session ID 一致性**：`/screenshot/respond` 的 `session_id` 必须与之前 `/chat` 请求的 `session_id` 完全一致，否则会话会过期。

2. **超时处理**：如果用户长时间未响应（超过几分钟），后端会话可能会被清理，此时调用 `/screenshot/respond` 会返回 `[系统]会话已过期`。

3. **多次截屏**：一次对话中 Agent 可能多次请求截屏，每次都会触发 `interrupt` 事件，前端需要多次处理确认流程。

4. **SSE 连接管理**：
   - 收到 `interrupt` 后原 SSE 流会终止
   - 确认后 `/screenshot/respond` 返回新的 SSE 流
   - 需要正确处理两个流的接续

---

## 6.测试接口

### GET /health

#### 接收参数：

- `session_id`：会话ID（query, str）

#### 返回结果：
成功：

```json
{
  "data": {
    "status": "ok",
    "model": "gemini-3.1-pro-preview"
  },
  "msg": "success",
  "code": 200
}
```
