"""Neo4j 图存储"""

import asyncio
import logging
from datetime import datetime
from typing import Any

from app.agent_v2.memory.base import Entity, Relation

logger = logging.getLogger(__name__)


class Neo4jStore:
    """Neo4j 图存储类

    负责：
    - 存储实体节点
    - 存储关系边
    - 图检索（实体关系推理）
    """

    def __init__(self, uri: str, user: str, password: str):
        self.uri = uri
        self.user = user
        self.password = password
        self._driver = None

    @property
    def driver(self):
        """延迟初始化驱动"""
        if self._driver is None:
            try:
                from neo4j import GraphDatabase
                self._driver = GraphDatabase.driver(
                    self.uri,
                    auth=(self.user, self.password),
                )
                # 创建索引
                self._create_indexes()
                logger.info("[Neo4jStore] 连接成功: %s", self.uri)
            except Exception as e:
                logger.warning("[Neo4jStore] 连接失败: %s", e)
                raise
        return self._driver

    def _create_indexes(self):
        """创建索引（提升查询性能）"""
        try:
            with self._driver.session() as session:
                # 实体名称索引
                session.run(
                    "CREATE INDEX entity_name_index IF NOT EXISTS FOR (e:Entity) ON (e.name)"
                )
                # 关系 session_id 索引
                session.run(
                    "CREATE INDEX relates_session_index IF NOT EXISTS FOR ()-[r:RELATES]-() ON (r.session_id)"
                )
                logger.debug("[Neo4jStore] 索引创建完成")
        except Exception as e:
            logger.warning("[Neo4jStore] 创建索引失败: %s", e)

    # ==================== 实体操作 ====================

    def upsert_entity_sync(self, entity: Entity) -> str:
        """插入或更新实体（同步）"""
        with self.driver.session() as session:
            result = session.run(
                """
                MERGE (e:Entity {name: $name})
                SET e.first_seen = coalesce(e.first_seen, $first_seen)
                RETURN e.name as name
                """,
                name=entity.name,
                first_seen=entity.first_seen.isoformat() if entity.first_seen else datetime.now().isoformat(),
            )
            record = result.single()
            return record["name"] if record else entity.name

    async def upsert_entity(self, entity: Entity) -> str:
        """插入或更新实体（异步封装）"""
        return await asyncio.to_thread(self.upsert_entity_sync, entity)

    # ==================== 关系操作 ====================

    def create_relation_sync(self, relation: Relation) -> None:
        """创建关系边（同步）"""
        with self.driver.session() as session:
            session.run(
                """
                MERGE (a:Entity {name: $subject})
                MERGE (b:Entity {name: $obj})
                WITH a, b
                MERGE (a)-[r:RELATES {memory_id: $memory_id}]->(b)
                SET r.relation = $relation,
                    r.time_note = $time_note,
                    r.is_single_value = $is_single_value,
                    r.session_id = $session_id,
                    r.is_current = $is_current,
                    r.created_at = $created_at
                """,
                subject=relation.subject,
                relation=relation.relation,
                obj=relation.obj,
                time_note=relation.time_note,
                is_single_value=relation.is_single_value,
                memory_id=relation.memory_id,
                session_id=relation.session_id,
                is_current=relation.is_current,
                created_at=relation.created_at.isoformat() if relation.created_at else datetime.now().isoformat(),
            )

        logger.debug(
            "[Neo4jStore] 创建关系: %s -[%s]-> %s",
            relation.subject, relation.relation, relation.obj
        )

    async def create_relation(self, relation: Relation) -> None:
        """创建关系边（异步封装）"""
        return await asyncio.to_thread(self.create_relation_sync, relation)

    def mark_obsolete_sync(self, memory_id: str) -> int:
        """标记关系过期（同步）"""
        with self.driver.session() as session:
            result = session.run(
                """
                MATCH ()-[r:RELATES {memory_id: $memory_id}]-()
                SET r.is_current = false
                RETURN count(r) as count
                """,
                memory_id=memory_id,
            )
            record = result.single()
            return record["count"] if record else 0

    async def mark_obsolete(self, memory_id: str) -> int:
        """标记关系过期（异步封装）"""
        return await asyncio.to_thread(self.mark_obsolete_sync, memory_id)

    # ==================== 查询操作 ====================

    def search_by_entity_sync(
        self,
        session_id: str,
        entities: list[str],
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """通过实体关联检索记忆（同步）"""
        with self.driver.session() as session:
            result = session.run(
                """
                MATCH (e:Entity)-[r:RELATES]->(related:Entity)
                WHERE (e.name IN $entities OR related.name IN $entities)
                  AND r.session_id = $session_id
                  AND r.is_current = true
                RETURN e.name as subject,
                       r.relation as relation,
                       related.name as obj,
                       r.time_note as time_note,
                       r.memory_id as memory_id,
                       r.is_single_value as is_single_value
                LIMIT $limit
                """,
                entities=entities,
                session_id=session_id,
                limit=limit,
            )
            return [dict(record) for record in result]

    async def search_by_entity(
        self,
        session_id: str,
        entities: list[str],
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """通过实体关联检索记忆（异步封装）"""
        return await asyncio.to_thread(
            self.search_by_entity_sync, session_id, entities, limit
        )

    def get_entity_relations_sync(
        self,
        session_id: str,
        entity_name: str,
        depth: int = 2,
    ) -> list[dict[str, Any]]:
        """获取实体的关系网络（同步）"""
        with self.driver.session() as session:
            result = session.run(
                f"""
                MATCH path = (e:Entity {{name: $entity_name}})-[r:RELATES*1..{depth}]-(related:Entity)
                WHERE all(rel IN r WHERE rel.session_id = $session_id AND rel.is_current = true)
                UNWIND r as rel
                RETURN startNode(rel).name as subject,
                       rel.relation as relation,
                       endNode(rel).name as obj,
                       rel.time_note as time_note,
                       rel.memory_id as memory_id
                """,
                entity_name=entity_name,
                session_id=session_id,
            )
            return [dict(record) for record in result]

    async def get_entity_relations(
        self,
        session_id: str,
        entity_name: str,
        depth: int = 2,
    ) -> list[dict[str, Any]]:
        """获取实体的关系网络（异步封装）"""
        return await asyncio.to_thread(
            self.get_entity_relations_sync, session_id, entity_name, depth
        )

    # ==================== 清理操作 ====================

    def delete_by_session_sync(self, session_id: str) -> int:
        """删除指定会话的所有关系（同步）"""
        with self.driver.session() as session:
            result = session.run(
                """
                MATCH ()-[r:RELATES {session_id: $session_id}]-()
                DELETE r
                RETURN count(r) as count
                """,
                session_id=session_id,
            )
            record = result.single()
            return record["count"] if record else 0

    async def delete_by_session(self, session_id: str) -> int:
        """删除指定会话的所有关系（异步封装）"""
        return await asyncio.to_thread(self.delete_by_session_sync, session_id)

    async def close(self) -> None:
        """关闭连接"""
        if self._driver:
            self._driver.close()
            self._driver = None
