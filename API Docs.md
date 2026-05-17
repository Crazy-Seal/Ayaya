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

```json
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

## 4.测试接口

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
