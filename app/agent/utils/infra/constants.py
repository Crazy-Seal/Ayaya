"""agent 跨模块共享常量。

只集中"被多个模块共用 / 此前在多处重复定义"的值；单模块私有的配置（截图压缩算法的
TTL 与数量阈值、run_ps 的超时、WORKDIR、各 DB 路径等）仍保留在各自模块，保持就近维护。

本模块不 import 任何 agent 模块（纯值），可被任意处安全 import，零循环风险。
"""

# ==================== 截图消息身份 ====================
# 原散落于 core/pipeline.py、domain/window.py、domain/text.py
SCREENSHOT_MESSAGE_NAME = "system_screenshot"
SCREENSHOT_COMPRESSED_NAME = "system_screenshot_compressed"

# ==================== 会话窗口尺寸 ====================
# domain 与 plugins 共用
MAX_HUMAN_MESSAGES_IN_CHECKPOINT = 20  # checkpoint 人类消息数量上限
SUMMARY_EVERY_HUMAN_MESSAGES = 10      # 每隔多少条人类消息总结一次
RECENT_CONTEXT_HUMAN_MESSAGES = 10     # 送模型的最近人类消息数量
SCREENSHOT_TTL_HUMAN_MESSAGES = 2      # 截图存活轮数，超过则压缩
MAX_SCREENSHOTS_IN_CONTEXT = 2         # 上下文中最多保留的截图数量

# ==================== VLM 默认配置 ====================
# 原散落于 models/vlm.py、utils/image_description.py
VLM_DEFAULT_MODEL = "qwen3-vl-plus"
VLM_DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

