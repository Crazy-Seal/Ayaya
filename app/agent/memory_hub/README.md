# MemoryHub 四层架构说明

`memory_hub` 已做结构替换：核心只保留与 `app/agent/memory` 同风格的四层主干，不再保留旧兼容壳。

## 1. 四层目录结构

```text
app/agent/memory_hub/
├── base.py                                 # BaseMemory / MemoryItem 抽象
├── manager.py                              # MemoryManager（核心调度）
├── types.py                                # MemoryEntry / MemoryContext / 请求事件
├── memory_types/                           # 记忆类型层
│   ├── short_term.py                       # 短期摘要记忆类型
│   ├── episodic.py                         # 情景记忆类型
│   ├── semantic.py                         # 语义记忆类型骨架
│   └── multimodal.py                       # 多模态记忆类型骨架
├── storage/                                # 存储后端层
│   ├── interfaces.py                       # 存储适配器协议
│   ├── registry.py                         # 存储适配器注册中心
│   ├── repositories.py                     # 仓储门面
│   └── adapters/
│       ├── sqlite_short_term_adapter.py    # 短期记忆 sqlite 实现
│       └── sqlite_vector_store_adapter.py  # 向量记忆 sqlite 实现
├── embedding/                              # 嵌入服务层
│   ├── interfaces.py                       # EmbeddingProvider 协议
│   ├── providers.py                        # OpenAI 兼容嵌入实现
│   └── registry.py                         # 嵌入服务注册中心
├── config.py                               # MemoryConfig
├── summarizers.py                          # 摘要/提取策略
├── __init__.py                             # 对外导出新规范入口
```

## 2. 启用方式

### 2.1 记忆类型启用

- 启用入口：`MemoryConfig.memory_plugins`
- 装配位置：`app/agent/memory_hub/manager.py`
- 默认启用：`short_term_default`、`episodic_default`
- 可选启用：`semantic_default`、`multimodal_default`

### 2.2 存储适配器注册

- 注册点：`app/agent/memory_hub/storage/registry.py`
- 短期记忆与向量存储分别维护 registry
- 目前默认后端为 `sqlite`

### 2.3 嵌入服务注册

- 注册点：`app/agent/memory_hub/embedding/registry.py`
- 根据 `EMBEDDING_PROVIDER` 选择 provider
- 默认 `openai_compatible`

## 3. 类与接口职责

- `MemoryManager`（核心层）
  - 统一 `recall(...)` / `persist(...)`
  - 只依赖 `user_id + MemoryConfig`
  - 聚合插件输出，抽取 `short_memory`
- `BaseMemory`（记忆类型接口）
  - `recall(messages, query_text, top_k) -> list[MemoryEntry]`
  - `persist(messages) -> None`
- `MemoryConfig`（配置模型）
  - 记忆系统唯一配置载体
  - 包含模型参数、`memory_plugins`、摘要记忆字数范围

## 4. 调用链

1. Agent 层通过 `build_default_memory_manager(...)` 获取 `MemoryManager`。
2. `MemoryManager` 从 `MemoryConfig` 解析并构建记忆类型实例。
3. `recall` 触发 `memory_types/*` 的检索逻辑并聚合。
4. `persist` 触发各记忆类型的异步写入逻辑，由 `storage` 层落库。
5. 向量索引所需嵌入由 `embedding` 层提供。

## 5. 新规范入口

- 构建入口：`build_default_memory_manager(chat_settings)`
- 核心对象：`MemoryManager`
- 统一调用：`MemoryManager.recall(...)` / `MemoryManager.persist(...)`
