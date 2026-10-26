import { resolveAiName } from "../../../ai-identity.js";

/**
 * 系统提示词构建工具
 */

/**
 * 提示词模板参数
 */
export interface PromptTemplateParams {
  name?: string | null;
  feature?: string | null;
  character?: string | null;
  address?: string | null;
  characteristic?: string | null;
  constraint?: string | null;
}

/**
 * 为 LLM 配置、表情预览和保存流程构建同一份系统提示词。
 *
 * Args:
 *     params (PromptTemplateParams): 助手名称、角色、称呼和行为约束组成的提示词模板数据。
 *     expressionLabels (string[]): 当前模型允许使用的表情标签名称。
 *
 * Returns:
 *     string: 包含角色描述、行为约束和可用表情标签的系统提示词。
 */
export function buildSystemPrompt(
  params: PromptTemplateParams,
  expressionLabels: string[] = []
): string {
  const name = resolveAiName(params.name);
  const feature = params.feature || "可爱";
  const character = params.character || "AI少女";
  const address = params.address || "主人";
  const characteristic = params.characteristic || "";
  const constraint = params.constraint || "";

  let prompt = `你是${name}，一个${feature}的${character}，称呼用户为${address}。`;
  if (characteristic) {
    prompt += `\n${characteristic}`;
  }
  if (constraint) {
    prompt += `\n${constraint}`;
  }

  // 1. 只有当前模型声明了表情标签，才向提示词加入输出标签说明。
  if (expressionLabels.length > 0) {
    const tagsList = expressionLabels.map(/**
     * 把可用表情名称转换为角色提示词中的合法输出标签。
     *
     * Args:
     *     l (string): 当前可用于提示词的表情名称。
     *
     * Returns:
     *     string: 带尖括号的表情标签。
     */
    (l) => `<${l}>`).join("");
    prompt += `\n你可以在对话中使用以下表情标签：${tagsList}，使用时必须像示例一样使用尖括号<>包裹`;
  }

  return prompt;
}
