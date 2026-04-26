定义：
日记：某一天所有的对话内容生成的摘要

# 进度追踪

| 模块 | 状态 | 备注 |
|------|------|------|
| MemoryManager | ✅ 完成 | |
| SummaryMemory | ✅ 完成 | 含摘要和日记功能 |
| EpisodicMemory | ✅ 完成 | 扁平化 Schema，综合评分检索 |
| SemanticMemory | ✅ 完成 | LLM 提取三元组，混合检索 |
| EpisodicSqliteStore | ✅ 完成 | |
| EpisodicChromaStore | ✅ 完成 | |
| DiarySqliteStore | ✅ 完成 | |
| ChatHistoryStore | ✅ 完成 | |
| SemanticSqliteStore | ✅ 完成 | |
| Neo4jStore | ✅ 完成 | |

---

# 管理层
## 总记忆管理类-MemoryManager：
每个MyAgent对象含有一个MemoryManager对象

**成员变量**：
- session_id: str
- 三种记忆的对象

**方法**：
- add(List[AnyMessage], history:List[AnyMessage]):添加记忆：将10轮聊天记录和5轮历史聊天记录（用于前情提要）交由情景记忆和语义记忆类处理，生成记忆
- get_context() → str:获取当前对话上下文：前两天摘要+今日摘要+相关情景记忆（3条）+相关语义记忆（3条），若无某部分则跳过
- search(query:str, type:str, top_k:int=3) → str:搜索相关记忆，返回相关记忆列表，type指定搜索哪种记忆（情景/语义）
- search_diary(start:Date, end:Date) → str:搜索时间范围内的日记，返回日记内容列表（最多间隔5天）

# 记忆层
## 摘要记忆类-SummaryMemory：
**成员变量**：
- session_id: str
- model: 总结模型对象

**方法**：
- add(date:Date):从聊天记录表中获取当天的对话内容+前两天的摘要（前两天指的是往前数有摘要的两天），交由大模型总结，生成摘要并保存至摘要表中
- search(date:Date) → str:获取指定日期的摘要内容
- get_context() → str:获取当前对话上下文：前两天摘要+今日摘要，若无某部分则跳过
- _get_recent_summaries(n:int=2) → str:获取最近的n天摘要内容，默认n=2（私有方法，被其它方法调用）

## 情景记忆类-EpisodicMemory ✅：

**架构**：双写模式（SQLite 权威数据源 + Chroma 向量索引）

**成员变量**：
- session_id: str
- sqlite_store: EpisodicSqliteStore，SQLite 权威数据源
- chroma_store: EpisodicChromaStore，Chroma 向量索引
- llm: ChatOpenAI，LLM 提取模型

**方法**：
- add(messages:str, history:str): 双写存储，LLLM 提取记忆（使用扁平化 Schema 避免 $defs/$ref 兼容性问题）
- search(query:str, top_k:int=3) → List[MemoryItem]: 向量检索 + 综合评分（向量相似度 * 0.8 + 近因分数 * 0.2）* 重要性权重
- get_timeline(start:Date, end:Date) → List[MemoryItem]: 时间线查询（从 SQLite 查询）
- delete(memory_id:str) → bool: 双删（SQLite + Chroma）
- delete_by_importance(importance_below:float) → int: 批量删除低重要度记忆
- delete_before_date(before_date:Date) → int: 批量删除早期记忆
- recover_from_sqlite() → int: 从 SQLite 恢复 Chroma 索引
- get_stats() → dict: 获取记忆统计信息
- count() → int: 获取记忆数量

---

## 语义记忆类-SemanticMemory ✅

### 架构设计

**三层存储架构**：
- **SQLite**：权威数据源，存储完整记忆记录
- **Chroma**：向量索引，支持语义检索
- **Neo4j**：实体关系图，支持图检索

**数据一致性策略**：
- SQLite 为权威数据源，Chroma/Neo4j 为索引
- 存储失败时回滚 SQLite（Chroma/Neo4j 失败则删除已存 SQLite 记录）
- 检索时二次验证 SQLite 的 `is_current` 状态，防止索引更新失败返回过期记忆

### 核心流程

#### 1. 添加记忆 add(messages, history)

```
输入: messages(当前聊天记录), history(历史聊天记录)
输出: 添加的记忆 ID 列表

流程:
1. LLM 提取语义知识 → _extract_memories()
2. 遍历每条记忆:
   2.1 检查重复 → 跳过或继续
   2.2 查找冲突 → 记录冲突列表
   2.3 存储到 SQLite
   2.4 存储到 Chroma + Neo4j（失败则回滚 SQLite）
   2.5 标记冲突记忆过期 → _mark_obsolete()
3. 返回成功添加的记忆 ID 列表
```

#### 2. LLM 提取 _extract_memories()

**提示词策略**：
- 角色设定：使用 chat_settings 中的 name、feature、character、address
- 提取目标：语义知识（事实、偏好、属性）
- 输出格式：JSON Schema（扁平化，避免 $defs/$ref）

**提取字段**：
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| content | string | ✅ | 记忆内容 |
| event_date | string | ✅ | 事件日期 (YYYY-MM-DD) |
| importance | float | ✅ | 重要性 (0.0-1.0) |
| subject | string | ❌ | 主体实体 |
| relation | string | ❌ | 关系 |
| object | string | ❌ | 客体实体 |
| time_note | string | ❌ | 时间状语 |
| is_single_value | boolean | ❌ | 是否单值属性 |

**命名规范**：
- 用户 → "用户"
- Agent → "我"
- 第三方 → 保持原名或关系描述（如"用户的老板"）

#### 3. 去重策略

**优先级**：
1. **三元组去重**（精确匹配）：`subject + relation + obj` 完全相同
   - 适用场景：有完整三元组的记忆
   - 实现：SQLite 查询 `find_by_triple()`

2. **内容相似度去重**（模糊匹配）：向量相似度 >= 0.95
   - 适用场景：无完整三元组的记忆
   - 实现：Chroma 向量检索 + SQLite 二次验证 `is_current`

#### 4. 冲突检测

**冲突定义**：同 session + 同 subject + 同 relation + 不同 obj + 当前有效 + 单值属性

**冲突处理策略**：
```
1. 先存储新记忆
2. 存储成功后，标记旧记忆过期
3. 过期信息：is_current=False, superseded_by=新记忆ID
```

**设计原则**：先存后标，避免存储失败导致数据丢失

#### 5. 存储流程

**SQLite 存储**：
```python
await sqlite_store.add(
    record_id, session_id, content, importance, event_date,
    subject, relation, obj, time_note, is_single_value
)
```

**Chroma 存储**：
```python
metadata = {
    "session_id": ...,
    "importance": ...,
    "event_date": ...,
    "is_single_value": 1/0,  # 布尔值转整数
    "is_current": 1,
    "created_at": ...,
    # 可选字段（非 None 时添加）
    "subject", "relation", "obj", "time_note"
}
await chroma_store.upsert(record_id, content, metadata)
```

**Neo4j 存储**（有完整三元组时）：
```python
# 1. 创建实体节点
await neo4j_store.upsert_entity(subject_entity)
await neo4j_store.upsert_entity(obj_entity)

# 2. 创建关系
relation = Relation(subject, relation, obj, ...)
await neo4j_store.create_relation(relation)
```

**失败回滚**：Chroma/Neo4j 存储失败时，删除已存入 SQLite 的记录

#### 6. 标记过期 _mark_obsolete()

```
流程:
1. SQLite 标记 is_current=False, superseded_by=新ID
2. Chroma 更新 metadata.is_current=0
3. Neo4j 更新关系 is_current=false

异常处理: Chroma/Neo4j 更新失败只记录日志，不中断流程
```

#### 7. 混合检索 search(query, top_k)

```
流程:
1. 向量检索 → _vector_search(top_k * 3)
2. 图检索 → _graph_search(top_k * 3)
3. 融合排序 → _combine_and_rank()
4. 返回 top_k 条记忆
```

#### 8. 向量检索 _vector_search()

```python
# 1. Chroma 向量检索
results = chroma_store.search(
    query=query,
    top_k=top_k,
    where={"session_id": ..., "is_current": 1}
)

# 2. 从 SQLite 获取完整记录（权威数据源）
# 3. 二次验证 is_current（防止 Chroma metadata 更新失败）
# 4. 计算向量得分: vector_score = 1.0 - distance
```

#### 9. 图检索 _graph_search()

```python
# 1. 提取查询实体
entities = _extract_query_entities(query)

# 2. Neo4j 图检索（同时匹配起点和终点实体）
results = neo4j_store.search_by_entity(
    session_id=session_id,
    entities=entities,
    limit=top_k
)
# Cypher: WHERE (e.name IN $entities OR related.name IN $entities)

# 3. 去重 + SQLite 获取完整记录
# 4. 二次验证 is_current
# 5. 图得分固定为 1.0
```

#### 10. 实体提取 _extract_query_entities()

**混合策略**：
1. **固定实体**（字符串匹配）：
   - "用户" 或 chat_settings.address → "用户"
   - "你" 或 chat_settings.name → "我"

2. **spaCy NER**（第三方实体）：
   - 模型：zh_core_web_sm
   - 类型过滤：PERSON, ORG, GPE, LOC, NORP, FAC
   - 线程安全：类级别缓存 + 线程锁

#### 11. 融合排序 _combine_and_rank()

**评分公式**：
```
初始分:
- 向量检索结果: vector_score * 0.7
- 图检索独有结果: 0.3
- 双重命中: vector_score * 0.7 + 0.3

重要性加权:
combined_score *= 0.8 + importance * 0.4

最终得分范围: 0.28 ~ 1.14
```

**排序**：按 combined_score 降序

---

# 存储层

## EpisodicSqliteStore ✅：
SQLite 权威数据源，存储情景记忆的完整数据

**表结构**：
- id: 主键（episodic_{UUID}）
- session_id: 会话 ID
- content: 记忆内容
- event_date: 事件发生日期（YYYY-MM-DD）
- importance: 重要性评分（0.0-1.0）
- created_at: 创建时间

**特性**：WAL 模式 + busy_timeout 并发控制

## EpisodicChromaStore ✅：
Chroma 向量索引，支持语义检索

**存储内容**：
- id: 关联 SQLite 记录 ID
- content: 记忆内容（用于向量化）
- metadata: {session_id, event_date, importance, created_at}

## DiarySqliteStore ✅：
存储摘要和日记（同一张表，is_diary 字段区分）

**表结构**：
- id: 主键
- session_id: 会话 ID
- date: 日期
- content: 内容
- is_diary: 是否为日记

## ChatHistoryStore ✅：
存储聊天历史记录，支持时区

## SemanticSqliteStore ✅：
语义记忆 SQLite 存储，权威数据源

**表结构**：
| 字段 | 类型 | 说明 |
|------|------|------|
| id | TEXT | 主键 (semantic_{UUID}) |
| session_id | TEXT | 会话 ID |
| content | TEXT | 记忆内容 |
| importance | REAL | 重要性 (0.0-1.0) |
| event_date | DATE | 事件日期 |
| created_at | TIMESTAMP | 创建时间 |
| subject | TEXT | 主体实体 |
| relation | TEXT | 关系 |
| obj | TEXT | 客体实体 |
| time_note | TEXT | 时间状语 |
| is_single_value | BOOLEAN | 是否单值属性 |
| is_current | BOOLEAN | 是否当前有效 |
| superseded_by | TEXT | 被哪条记忆取代 |

**索引**：
- idx_semantic_session: (session_id)
- idx_semantic_subject: (session_id, subject)
- idx_semantic_conflict: (session_id, subject, relation)
- idx_semantic_current: (session_id, is_current)

**核心方法**：
- `find_conflicting()`: 查找冲突记忆（单值属性冲突检测）
- `find_by_triple()`: 按三元组查询（去重检测）
- `find_by_subject()`: 按主体查询

## Neo4jStore ✅：
Neo4j 图存储，支持实体关系检索

**节点**：Entity {name, first_seen}

**关系**：RELATES {memory_id, session_id, relation, time_note, is_single_value, is_current, created_at}

**索引**：
- entity_name_index: Entity.name
- relates_session_index: RELATES.session_id

**核心方法**：
- `upsert_entity()`: 创建/更新实体（MERGE 去重）
- `create_relation()`: 创建关系（MERGE + memory_id 唯一标识）
- `mark_obsolete()`: 标记关系过期
- `search_by_entity()`: 按实体检索（同时匹配起点和终点）

---

# 数据实体

## MemoryItem ✅：
记忆实体类

**字段**：
- id: str，唯一标识符
- session_id: str，会话 ID
- content: str，记忆内容
- timestamp: datetime，事件发生日期
- created_at: datetime，记录创建时间
- importance: float，重要性评分（0.0-1.0）
- metadata: dict，元数据

## Entity ✅：
实体类

**字段**：
- name: str，实体名称
- first_seen: datetime，首次出现时间

## Relation ✅：
关系类

**字段**：
- subject: str，主体实体名称
- relation: str，关系
- obj: str，客体实体名称
- time_note: str，时间状语
- is_single_value: bool，是否单值属性
- memory_id: str，关联的记忆 ID
- session_id: str，会话 ID
- is_current: bool，是否当前有效
- created_at: datetime，创建时间

---

# 设计决策记录

## 1. 为什么用 `obj` 而不是 `object`？

`object` 是 SQL 保留字，虽然在 SQLite 中使用参数化查询不会出错，但在某些 SQL 工具或迁移脚本中可能引发问题。统一使用 `obj` 避免潜在风险。

## 2. 为什么布尔值转整数存 Chroma？

某些 Chroma 版本不支持布尔值 metadata，转换为整数 (1/0) 提高兼容性。

## 3. 为什么图检索独有结果评分是 0.5？

图检索命中意味着实体精确匹配，本身有较高相关性。评分范围设计：
- 向量检索：0.24 ~ 0.84（经过重要性加权）
- 图检索独有：0.40 ~ 0.60
- 双重命中：0.54 ~ 1.14（最高优先级）

## 4. 为什么需要 SQLite 二次验证？

防止 Chroma/Neo4j metadata 更新失败导致返回过期记忆。SQLite 是权威数据源，检索结果必须与 SQLite 状态一致。

## 5. 为什么存储失败要回滚 SQLite？

保证三层存储数据一致性。如果 Chroma/Neo4j 存储失败，已存入 SQLite 的记录需要删除，避免"僵尸数据"（SQLite 有但索引没有）。

## 6. 为什么先存后标而不是先标后存？

避免存储失败导致数据丢失。如果先标记旧记忆过期，新记忆存储失败，则旧记忆丢失。改为先存新记忆，成功后再标记旧记忆过期。
