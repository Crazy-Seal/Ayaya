from pydantic import BaseModel

class ChatSettings(BaseModel):
    # 会话 ID，用于区分前端不同的助手
    session_id: str
    # 大模型名称（OpenAI 兼容接口）
    model_name: str
    # 大模型 API Key
    openai_api_key: str
    # 可选：OpenAI 兼容网关地址（例如第三方平台）
    openai_base_url: str
    # 模型温度
    temperature: float
    # 系统提示词
    system_prompt: str
    # 工具列表
    tools_list: list[str]
    # 可选：启用的记忆插件列表（按顺序执行）
    memory_plugins: list[str] | None = None
    # 可选：启用的能力包（Skill）列表（仅 agent 使用）
    skills: list[str] | None = None

    # === 提示词模板字段 ===
    # AI 名字，如 "日和"
    name: str | None = None
    # AI 性格特点，如 "可爱"
    feature: str | None = None
    # AI 人设，如 "AI少女"
    character: str | None = None
    # 对用户称呼，如 "主人"
    address: str | None = None
    # 详细性格描述
    characteristic: str | None = None
    # 发言约束
    constraint: str | None = None

    def __hash__(self):
        return hash((self.session_id,
                     self.model_name,
                     self.openai_api_key,
                     self.openai_base_url,
                     self.temperature,
                     self.system_prompt,
                     tuple(self.tools_list),
                     tuple(self.memory_plugins or []),
                     tuple(self.skills or [])))
