from pathlib import Path


# 记忆相关 sqlite 文件路径。
STORE_DB_PATH = Path(__file__).resolve().parents[3] / "memory" / "sqlite" / "store.sqlite3"
CHECKPOINT_DB_PATH = Path(__file__).resolve().parents[3] / "memory" / "sqlite" / "checkpoints.sqlite3"

# 长期记忆相似度阈值，高于该值会触发融合写回。
LONG_MEMORY_MERGE_SIMILARITY_THRESHOLD = 0.9

# 总结窗口：之前 5 轮 + 最近 10 轮用户消息。
PREVIOUS_HUMAN_MESSAGES_FOR_SUMMARY = 5
LATER_HUMAN_MESSAGES_FOR_SUMMARY = 10

