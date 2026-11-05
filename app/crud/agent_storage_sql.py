"""父子执行状态共用的固定建表定义，供 DAO 初始化与离线迁移使用。"""

from app.agent.state.messages import PROTOCOL_MESSAGE_TYPES
from app.schemas.agent_storage import AgentStorageScope


def agent_state_schema(scope: AgentStorageScope) -> tuple[str, ...]:
    """返回对应范围的检查点、协议历史及索引，不创建连接或提交事务。"""
    checkpoints, messages = scope.checkpoints_table, scope.messages_table
    allowed = ",".join(f"'{value}'" for value in PROTOCOL_MESSAGE_TYPES)
    return (
        f"CREATE TABLE IF NOT EXISTS {checkpoints} (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "session_id TEXT NOT NULL, state_json TEXT NOT NULL, checkpoint_type TEXT NOT NULL DEFAULT 'completed' "
        "CHECK(checkpoint_type IN ('intermediate','completed')), created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
        f"CREATE INDEX IF NOT EXISTS idx_{checkpoints}_session_type_id ON {checkpoints}(session_id,checkpoint_type,id DESC)",
        f"CREATE TABLE IF NOT EXISTS {messages} (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, "
        "message_id TEXT NOT NULL, created_at TEXT NOT NULL, record_kind TEXT NOT NULL CHECK(record_kind IN ('protocol','legacy_tool_notice')), "
        "fidelity TEXT NOT NULL CHECK(fidelity IN ('exact','projected')), legacy_source_id TEXT, "
        f"message_type TEXT CHECK(message_type IN ({allowed})), message_json TEXT NOT NULL, UNIQUE(session_id,message_id))",
        f"CREATE INDEX IF NOT EXISTS idx_{messages}_session_id ON {messages}(session_id,id)",
        f"CREATE INDEX IF NOT EXISTS idx_{messages}_session_time ON {messages}(session_id,created_at,id)",
    )
