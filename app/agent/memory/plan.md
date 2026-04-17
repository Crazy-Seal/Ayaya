定义：
日记：某一天所有的对话内容生成的摘要

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

## 情景记忆类-EpisodicMemory：
**成员变量**：
- session_id: str
- vector_store: ChromaStore，向量数据库存储对象
- model: 总结模型对象

**方法**：
- add(messages:str, history:str): 将聊天记录交由大模型总结，生成记忆条目（大模型总结出时间元数据）并保存至向量数据库（带id和时间元数据，id设置为episodic_{UUID}）。
- search(query:str, top_k:int=3) → List[Memory]:使用向量数据库检索相关记忆，返回相关记忆列表，top_k指定返回多少条相关记忆
- update(messages:str, history:str): 更新记忆：

## 语义记忆类-SemanticMemory：
**成员变量**：
- session_id: str
- vector_store: ChromaStore，向量数据库存储对象
- graph_store: Neo4jStore，图数据库存储对象
- model: 总结模型对象

**方法**：
- add(messages:str, history:str): 将聊天记录交由大模型总结，生成记忆条目（大模型总结出时间元数据）并保存至两个数据库（带id和时间元数据，id设置为semantic_{UUID}），同时抽取实体/关系保存至图数据库
- _extract_memories(messages:str, history:str) → List[str]: 将聊天记录交由大模型总结，生成记忆条目（私有方法，被add方法调用）
- _extract_entities(messages:List[str]) → List[str]: 用spaCy从记忆条目中抽取实体（私有方法，被add方法调用）
- _extract_relations(messages:List[str], entities:List[Tuple[str, str, str]]) → List[Tuple[str, str, str]]: 用spaCy从记忆条目中抽取关系（私有方法，被add方法调用）
- _search_vector(query:str, top_k:int=3) → str:使用向量数据库检索记忆，返回相关记忆列表，top_k指定返回多少条记忆
- _search_graph(query:str, top_k:int=3) → str:使用图数据库检索记忆，返回相关记忆列表，top_k指定返回多少条记忆
- search(query:str, top_k:int=3) → str:综合使用向量数据库和图数据库检索相关记忆，根据公式整合结果，返回相关记忆列表，top_k指定返回多少条相关记忆

# 存储层
## ChromaStore类：向量数据库存储类

## Neo4jStore类：图数据库存储类

## SqliteStore类：关系数据库存储类，负责存储摘要记忆和日记内容，表设计：摘要记忆和日记存储在同一张表内，使用一个is_diary字段区分，表字段如下：
- id: 主键，唯一标识符（自动生成）
- session_id: 会话ID，关联MyAgent对象（非空）
- date: 日期，该条摘要或日记的日期（非空唯一）
- content: 内容，存储摘要或日记的文本内容（非空）
- is_diary: 布尔值，标识该记录是摘要（False）还是日记（True）（非空）

# 数据实体
## Memory类：记忆实体类，包含以下字段：
- id: str，唯一标识符，格式为"{type}_{UUID}"，其中type为记忆类型（episodic或semantic），UUID为随机生成的唯一ID
- session_id: str，关联MyAgent对象的会话ID
- content: str，记忆内容
- timestamp: datetime，记忆生成的时间戳
- type: str，记忆类型，取值为"episodic"或"semantic"