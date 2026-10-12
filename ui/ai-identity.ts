/** 未填写 LLM 角色名字时使用的项目默认称呼，与 Live2D 资源名字无关。 */
export const DEFAULT_AI_NAME = "彩夜";

/**
 * 将已保存或正在编辑的 AI 名字转换成标题、卡片和系统提示词共同使用的称呼。
 *
 * Args:
 *     name (string | null | undefined): LLM 配置中的角色名字；空白表示使用项目默认称呼。
 *
 * Returns:
 *     string: 去除首尾空白的角色名字，未填写时为彩夜。
 */
export const resolveAiName = (name?: string | null): string => name?.trim() || DEFAULT_AI_NAME;
