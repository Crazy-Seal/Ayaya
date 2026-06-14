"""语义记忆 - 存储抽象知识，支持实体关系推理"""

import logging
import threading
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.agent_v2.memory.base import Entity, MemoryItem, Relation
from app.agent_v2.memory.config import MemoryConfig
from app.agent_v2.memory.store.episodic_chroma_store import EpisodicChromaStore
from app.agent_v2.memory.store.neo4j_store import Neo4jStore
from app.agent_v2.memory.store.semantic_sqlite_store import SemanticSqliteStore
from app.agent_v2.utils.llm_client import LLMClient, LLMConfig
from app.schemas.chat_settings import ChatSettings

logger = logging.getLogger(__name__)


# ==================== 结构化输出模型 ====================

class SemanticMemoryItem(BaseModel):
    """单条语义记忆"""
    content: str = Field(description="记忆内容")
    event_date: str = Field(description="事件发生日期")
    importance: float = Field(description="记忆重要性评分（0.0-1.0）", ge=0.0, le=1.0)
    subject: str | None = Field(default=None, description="主体实体")
    relation: str | None = Field(default=None, description="关系")
    object: str | None = Field(default=None, description="客体实体")
    time_note: str | None = Field(default=None, description="时间状语")
    is_single_value: bool = Field(default=True, description="是否为单值属性")


class SemanticMemoryOutput(BaseModel):
    """语义记忆提取结果"""
    memories: list[SemanticMemoryItem] = Field(
        description="提取的记忆列表",
        min_length=0,
        max_length=10,
    )


# 扁平化 JSON Schema
SEMANTIC_MEMORY_SCHEMA = {
    "type": "object",
    "properties": {
        "memories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "记忆内容"},
                    "event_date": {"type": "string", "format": "date", "description": "记忆产生日期"},
                    "importance": {
                        "type": "number",
                        "minimum": 0.0,
                        "maximum": 1.0,
                        "description": "记忆重要性评分（0.0-1.0）"
                    },
                    "subject": {"type": "string", "description": "主体实体（如：用户、我、用户的老板）"},
                    "relation": {"type": "string", "description": "关系（如：是、喜欢、在、职业）"},
                    "object": {"type": "string", "description": "客体实体"},
                    "time_note": {"type": "string", "description": "时间状语（如：小时候、去年）"},
                    "is_single_value": {"type": "boolean", "description": "是否为单值属性（如职业是单值，爱好是多值）"},
                },
                "required": ["content", "event_date", "importance"]
            },
            "minItems": 0,
            "maxItems": 10,
            "description": "提取的记忆列表"
        }
    },
    "required": ["memories"],
    "additionalProperties": False
}


class SemanticMemory:
    """语义记忆类

    职责：
    - 存储抽象知识和概念
    - LLM 提取实体关系三元组
    - 混合检索（向量 + 图融合）

    存储架构：
    - SQLite：权威数据源
    - Chroma：向量索引
    - Neo4j：实体关系图
    """

    NAMESPACE = "semantic_memory"

    # 类级别的 spaCy 模型缓存
    _nlp = None
    _nlp_lock = threading.Lock()

    def __init__(
        self,
        session_id: str,
        config: MemoryConfig,
        chat_settings: ChatSettings,
    ):
        self.session_id = session_id
        self.config = config
        self.chat_settings = chat_settings

        # 初始化 SQLite 存储（权威数据源）
        self.sqlite_store = SemanticSqliteStore(
            db_path=config.sqlite_path,
        )

        # 初始化 Chroma 向量存储
        self.chroma_store = EpisodicChromaStore(
            collection_name=f"{self.NAMESPACE}_{session_id}",
            embedding_config={
                "api_key": config.embedding_api_key,
                "model": config.embedding_model,
                "dimension": config.embedding_dimension,
                "base_url": config.embedding_base_url,
            },
            persist_path=config.chroma_path,
        )

        # 初始化 Neo4j 图存储
        self.neo4j_store = Neo4jStore(
            uri=config.neo4j_uri,
            user=config.neo4j_user,
            password=config.neo4j_password,
        )

        # 初始化 LLM
        self.llm = LLMClient(LLMConfig(
            model=chat_settings.model_name,
            api_key=chat_settings.openai_api_key,
            base_url=chat_settings.openai_base_url,
            temperature=0.1,
            timeout=60.0,
        ))

    # ==================== 核心方法 ====================

    async def add(self, messages: str, history: str) -> list[str]:
        """添加语义记忆

        将聊天记录交由 LLM 提取语义知识，存储到三层存储

        Args:
            messages: 当前聊天记录
            history: 历史聊天记录

        Returns:
            记忆 ID 列表
        """
        if not messages.strip():
            return []

        # 1. LLM 提取语义知识
        memory_items = await self._extract_memories(messages, history)

        # 2. 存储每条记忆
        added_ids = []
        for item in memory_items:
            memory_id = f"semantic_{uuid.uuid4().hex[:16]}"

            # 2.1 检查重复
            if (item.get("subject") and item.get("relation") and item.get("obj")):
                if await self._is_triple_duplicate(
                    item["subject"], item["relation"], item["obj"]
                ):
                    logger.debug(
                        "[SemanticMemory] 跳过三元组重复记忆: %s-%s-%s",
                        item["subject"], item["relation"], item["obj"]
                    )
                    continue
            elif await self._is_duplicate(item["content"]):
                content_preview = item["content"][:30] if len(item["content"]) > 30 else item["content"]
                logger.debug("[SemanticMemory] 跳过内容重复记忆: %s", content_preview)
                continue

            # 2.2 查找冲突
            conflicts = []
            if (item.get("subject") and item.get("relation") and item.get("obj")
                    and item.get("is_single_value", True)):
                conflicts = await self.sqlite_store.find_conflicting(
                    session_id=self.session_id,
                    subject=item["subject"],
                    relation=item["relation"],
                    exclude_obj=item["obj"],
                )

            # 2.3 存储到 SQLite
            try:
                await self.sqlite_store.add(
                    record_id=memory_id,
                    session_id=self.session_id,
                    content=item["content"],
                    importance=item["importance"],
                    event_date=item["event_date"],
                    subject=item.get("subject"),
                    relation=item.get("relation"),
                    obj=item.get("obj"),
                    time_note=item.get("time_note"),
                    is_single_value=item.get("is_single_value", True),
                )
            except Exception as e:
                logger.warning("[SemanticMemory] SQLite 存储失败，跳过此条记忆: %s", e)
                continue

            # 2.4 存储到 Chroma 和 Neo4j
            try:
                metadata = {
                    "session_id": self.session_id,
                    "importance": item["importance"],
                    "event_date": item["event_date"],
                    "is_single_value": 1 if item.get("is_single_value", True) else 0,
                    "is_current": 1,
                    "created_at": datetime.now().isoformat(),
                }
                for key, value in [
                    ("subject", item.get("subject")),
                    ("relation", item.get("relation")),
                    ("obj", item.get("obj")),
                    ("time_note", item.get("time_note")),
                ]:
                    if value is not None:
                        metadata[key] = value

                await self.chroma_store.upsert(
                    record_id=memory_id,
                    content=item["content"],
                    metadata=metadata,
                )

                if item.get("subject") and item.get("relation") and item.get("obj"):
                    await self._store_to_neo4j(memory_id, item)

            except Exception as e:
                logger.warning("[SemanticMemory] Chroma/Neo4j 存储失败，回滚 SQLite: %s", e)
                await self.sqlite_store.delete(memory_id)
                continue

            # 2.5 标记冲突记忆过期
            for old_id in conflicts:
                await self._mark_obsolete(old_id, memory_id)

            added_ids.append(memory_id)
            logger.info(
                "[SemanticMemory] 添加记忆: id=%s, subject=%s, relation=%s, obj=%s",
                memory_id, item.get("subject"), item.get("relation"), item.get("obj")
            )

        return added_ids

    async def search(self, query: str, top_k: int = 5) -> list[MemoryItem]:
        """混合检索相关记忆"""
        if not query.strip():
            return []

        # 1. 向量检索
        vector_results = await self._vector_search(query, top_k * 3)

        # 2. 图检索
        graph_results = await self._graph_search(query, top_k * 3)

        # 3. 融合排序
        combined = self._combine_and_rank(vector_results, graph_results, query)

        return combined[:top_k]

    async def get_by_subject(self, subject: str) -> list[MemoryItem]:
        """按主体查询记忆"""
        records = await self.sqlite_store.find_by_subject(
            session_id=self.session_id,
            subject=subject,
            is_current=True,
        )

        memory_items = []
        for r in records:
            memory_items.append(self._record_to_memory_item(r))

        return memory_items

    # ==================== 存储辅助方法 ====================

    async def _is_duplicate(self, content: str, threshold: float = 0.95) -> bool:
        """检查内容是否重复"""
        results = await self.chroma_store.search(
            query=content,
            top_k=1,
            where={
                "$and": [
                    {"session_id": self.session_id},
                    {"is_current": 1},
                ]
            },
        )
        if not results:
            return False

        memory_id = results[0]["id"]
        record = await self.sqlite_store.get(memory_id)
        if not record or not record.get("is_current", True):
            return False

        distance = results[0].get("distance", 1.0)
        similarity = 1.0 - distance
        return similarity >= threshold

    async def _is_triple_duplicate(
        self,
        subject: str,
        relation: str,
        obj: str,
    ) -> bool:
        """检查三元组是否重复"""
        existing = await self.sqlite_store.find_by_triple(
            session_id=self.session_id,
            subject=subject,
            relation=relation,
            obj=obj,
        )
        return existing is not None

    async def _mark_obsolete(self, old_id: str, new_id: str):
        """标记记忆过期"""
        await self.sqlite_store.update(
            record_id=old_id,
            is_current=False,
            superseded_by=new_id,
        )

        try:
            old_record = await self.sqlite_store.get(old_id)
            if old_record:
                metadata = {
                    "session_id": old_record["session_id"],
                    "importance": old_record["importance"],
                    "event_date": old_record["event_date"],
                    "is_single_value": 1 if old_record.get("is_single_value", True) else 0,
                    "is_current": 0,
                    "created_at": old_record["created_at"],
                }
                for key, value in [
                    ("subject", old_record.get("subject")),
                    ("relation", old_record.get("relation")),
                    ("obj", old_record.get("obj")),
                    ("time_note", old_record.get("time_note")),
                ]:
                    if value is not None:
                        metadata[key] = value

                await self.chroma_store.upsert(
                    record_id=old_id,
                    content=old_record["content"],
                    metadata=metadata,
                )
        except Exception as e:
            logger.warning("[SemanticMemory] Chroma 更新过期状态失败: %s", e)

        try:
            await self.neo4j_store.mark_obsolete(old_id)
        except Exception as e:
            logger.warning("[SemanticMemory] Neo4j 标记过期失败: %s", e)

        logger.info("[SemanticMemory] 标记记忆过期: old_id=%s, new_id=%s", old_id, new_id)

    async def _store_to_neo4j(self, memory_id: str, item: dict):
        """存储到 Neo4j"""
        try:
            subject_entity = Entity(name=item["subject"])
            obj_entity = Entity(name=item["obj"])
            await self.neo4j_store.upsert_entity(subject_entity)
            await self.neo4j_store.upsert_entity(obj_entity)

            relation = Relation(
                subject=item["subject"],
                relation=item["relation"],
                obj=item["obj"],
                time_note=item.get("time_note"),
                is_single_value=item.get("is_single_value", True),
                memory_id=memory_id,
                session_id=self.session_id,
                is_current=True,
                created_at=datetime.now(),
            )
            await self.neo4j_store.create_relation(relation)

        except Exception as e:
            logger.warning("[SemanticMemory] Neo4j 存储失败: %s", e)

    def _record_to_memory_item(self, record: dict) -> MemoryItem:
        """将数据库记录转换为 MemoryItem"""
        return MemoryItem(
            id=record["id"],
            session_id=record["session_id"],
            content=record["content"],
            timestamp=datetime.strptime(record["event_date"], "%Y-%m-%d"),
            created_at=datetime.fromisoformat(record["created_at"]),
            importance=record["importance"],
            metadata={
                "subject": record.get("subject"),
                "relation": record.get("relation"),
                "obj": record.get("obj"),
                "time_note": record.get("time_note"),
                "is_single_value": record.get("is_single_value"),
                "is_current": record.get("is_current"),
            },
        )

    # ==================== LLM 提取 ====================

    async def _extract_memories(
        self,
        messages: str,
        history: str,
    ) -> list[dict[str, Any]]:
        """使用 LLM 提取语义知识"""
        prompt = self._build_extraction_prompt(messages, history)

        try:
            result = await self.llm.ainvoke_structured(
                messages=[{"role": "user", "content": prompt}],
                schema=SEMANTIC_MEMORY_SCHEMA,
            )

            validated = SemanticMemoryOutput.model_validate(result)

            memory_items = []
            for item in validated.memories:
                memory_items.append({
                    "content": item.content,
                    "event_date": item.event_date,
                    "importance": item.importance,
                    "subject": item.subject,
                    "relation": item.relation,
                    "obj": item.object,
                    "time_note": item.time_note,
                    "is_single_value": item.is_single_value,
                })

            return memory_items

        except Exception as e:
            logger.exception("[SemanticMemory] LLM 提取失败: %s", e)
            return []

    def _build_extraction_prompt(self, messages: str, history: str) -> str:
        """构建提取提示词"""
        return f"""你是{self.chat_settings.name}，一个{self.chat_settings.feature}的{self.chat_settings.character}，称呼用户为{self.chat_settings.address}。

请根据给定的对话历史记录和对话内容，提取**语义知识**。

**语义知识**是指关于用户或你自己的既定事实、偏好、属性等信息。例如：
- 用户的职业、爱好、性格特点
- 用户与他人的关系
- 用户的生活状态、习惯
- 你对用户的了解

## 提取要求
1. 每条记忆提取一个对应的三元组
2. 提取主体（subject）、关系（relation）、客体（object）三元组
3. 判断是否为单值属性：
   - 单值属性（is_single_value=true）：职业、配偶、居住地等（一个主体只能有一个）
   - 多值属性（is_single_value=false）：爱好、朋友、技能等（一个主体可以有多个）
4. 如有时间状语（如"小时候"、"去年"），请提取到 time_note 字段
5. 评估重要性（0.0-1.0）：
   - 0.8-1.0：核心信息（职业、家庭状况等）
   - 0.6-0.8：重要偏好和特点
   - 0.4-0.6：一般信息
   - 0.2 以下：琐碎信息（无需提取）
6. 可提取 0-10 条记忆，实事求是，忽略无关内容，禁止歪曲语义，没有就是没有，有几条就返回几条

## 命名规范（重要！）
- 提到用户时，统一使用"用户"
- 提到你自己时，统一使用"我"
- 第三方如有原名，保持原名，否则以关系描述（如"用户的老板"）作为实体名称

## 示例
对话："我是个程序员，平时喜欢打篮球"
输出：
{{"memories": [
  {{"content": "用户是程序员", "event_date": "2025-04-26", "importance": 0.8, "subject": "用户", "relation": "职业", "object": "程序员", "is_single_value": true}},
  {{"content": "用户喜欢打篮球", "event_date": "2025-04-26", "importance": 0.5, "subject": "用户", "relation": "爱好", "object": "打篮球", "is_single_value": false}}
]}}

## 历史记录（无需提取，仅供参考）
{history if history else "暂无"}

## 当前对话（需要提取）
{messages}"""

    # ==================== 检索方法 ====================

    async def _vector_search(self, query: str, top_k: int) -> list[MemoryItem]:
        """向量检索"""
        results = await self.chroma_store.search(
            query=query,
            top_k=top_k,
            where={
                "$and": [
                    {"session_id": self.session_id},
                    {"is_current": 1},
                ]
            },
        )

        memory_items = []
        for result in results:
            memory_id = result["id"]
            distance = result.get("distance", 0.5)

            record = await self.sqlite_store.get(memory_id)
            if not record:
                continue

            if not record.get("is_current", True):
                continue

            item = self._record_to_memory_item(record)
            item.metadata["vector_score"] = 1.0 - distance
            memory_items.append(item)

        return memory_items

    async def _graph_search(self, query: str, top_k: int) -> list[MemoryItem]:
        """图检索"""
        entities = self._extract_query_entities(query)

        if not entities:
            return []

        try:
            results = await self.neo4j_store.search_by_entity(
                session_id=self.session_id,
                entities=entities,
                limit=top_k,
            )

            seen_ids = set()
            memory_items = []
            for graph_item in results:
                memory_id = graph_item.get("memory_id")
                if not memory_id or memory_id in seen_ids:
                    continue
                seen_ids.add(memory_id)

                record = await self.sqlite_store.get(memory_id)
                if record and record.get("is_current", True):
                    item = self._record_to_memory_item(record)
                    item.metadata["graph_score"] = 1.0
                    memory_items.append(item)

            return memory_items

        except Exception as e:
            logger.warning("[SemanticMemory] 图检索失败: %s", e)
            return []

    @classmethod
    def _get_nlp(cls):
        """延迟加载 spaCy 模型"""
        if cls._nlp is None:
            with cls._nlp_lock:
                if cls._nlp is None:
                    try:
                        import spacy
                        cls._nlp = spacy.load("zh_core_web_sm")
                        logger.info("[SemanticMemory] spaCy 模型加载成功: zh_core_web_sm")
                    except Exception as e:
                        logger.warning("[SemanticMemory] spaCy 模型加载失败: %s", e)
                        cls._nlp = False
        return cls._nlp if cls._nlp else None

    def _extract_query_entities(self, query: str) -> list[str]:
        """从查询中提取实体名称"""
        entities = []

        # 固定实体
        if "用户" in query or self.chat_settings.address in query:
            entities.append("用户")

        if self.chat_settings.name in query or "你" in query:
            entities.append("我")

        # spaCy NER
        nlp = self._get_nlp()
        if nlp is not None:
            try:
                doc = nlp(query)
                for ent in doc.ents:
                    if ent.label_ in ("PERSON", "ORG", "GPE", "LOC", "NORP", "FAC"):
                        entities.append(ent.text)
            except Exception as e:
                logger.debug("[SemanticMemory] spaCy 实体提取失败: %s", e)

        return list(set(entities))

    def _combine_and_rank(
        self,
        vector_results: list[MemoryItem],
        graph_results: list[MemoryItem],
        query: str,
    ) -> list[MemoryItem]:
        """融合排序"""
        combined: dict[str, MemoryItem] = {}

        for item in vector_results:
            combined[item.id] = item
            item.metadata["combined_score"] = item.metadata.get("vector_score", 0.5) * 0.7

        for item in graph_results:
            if item.id in combined:
                combined[item.id].metadata["combined_score"] += 0.3
                combined[item.id].metadata["graph_score"] = 1.0
            else:
                item.metadata["combined_score"] = 0.3
                item.metadata["graph_score"] = 1.0
                combined[item.id] = item

        for item in combined.values():
            importance_weight = 0.8 + (item.importance * 0.4)
            item.metadata["combined_score"] *= importance_weight

        sorted_items = sorted(
            combined.values(),
            key=lambda x: x.metadata.get("combined_score", 0),
            reverse=True
        )

        return sorted_items

    # ==================== 统计方法 ====================

    async def count(self) -> int:
        """获取记忆数量"""
        return await self.sqlite_store.count(session_id=self.session_id, is_current=True)

    async def get_stats(self) -> dict[str, Any]:
        """获取统计信息"""
        return await self.sqlite_store.get_stats(session_id=self.session_id)
