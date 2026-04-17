"""Neo4j 图存储"""

from typing import Any

from app.agent.memory.base import Entity, Relation


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
            from neo4j import GraphDatabase
            self._driver = GraphDatabase.driver(
                self.uri,
                auth=(self.user, self.password),
            )
        return self._driver

    async def create_entity(self, entity: Entity) -> str:
        """创建实体节点

        Args:
            entity: 实体对象

        Returns:
            实体ID
        """
        raise NotImplementedError

    async def create_relation(self, relation: Relation) -> None:
        """创建关系边

        Args:
            relation: 关系对象
        """
        raise NotImplementedError

    async def upsert_entity(self, entity: Entity) -> str:
        """插入或更新实体

        Args:
            entity: 实体对象

        Returns:
            实体ID
        """
        raise NotImplementedError

    async def search_entities(
        self,
        query_entities: list[str],
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """通过实体名称检索相关记忆

        Args:
            query_entities: 查询实体名称列表
            limit: 返回数量

        Returns:
            相关实体和关系列表
        """
        raise NotImplementedError

    async def get_entity_relations(
        self,
        entity_name: str,
        depth: int = 2,
    ) -> list[dict[str, Any]]:
        """获取实体的关系网络

        Args:
            entity_name: 实体名称
            depth: 关系深度

        Returns:
            关系列表
        """
        raise NotImplementedError

    async def close(self) -> None:
        """关闭连接"""
        if self._driver:
            self._driver.close()
