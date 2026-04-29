/**
 * LLM 配置页面
 */

import type { ChatSettingsState } from "../types.js";

/**
 * LLM 配置页面管理器
 */
export class LlmPage {
  private baseUrlInput: HTMLInputElement;
  private apiKeyInput: HTMLInputElement;
  private modelNameInput: HTMLInputElement;
  private temperatureInput: HTMLInputElement;
  private systemPromptInput: HTMLTextAreaElement;
  private confirmBtn: HTMLButtonElement;

  // 提示词模板字段
  private nameInput: HTMLInputElement;
  private featureInput: HTMLInputElement;
  private characterInput: HTMLInputElement;
  private addressInput: HTMLInputElement;
  private characteristicInput: HTMLTextAreaElement;
  private constraintInput: HTMLTextAreaElement;

  private chatSettingsState: ChatSettingsState | null = null;

  constructor(
    baseUrlInput: HTMLInputElement,
    apiKeyInput: HTMLInputElement,
    modelNameInput: HTMLInputElement,
    temperatureInput: HTMLInputElement,
    systemPromptInput: HTMLTextAreaElement,
    confirmBtn: HTMLButtonElement,
    nameInput: HTMLInputElement,
    featureInput: HTMLInputElement,
    characterInput: HTMLInputElement,
    addressInput: HTMLInputElement,
    characteristicInput: HTMLTextAreaElement,
    constraintInput: HTMLTextAreaElement
  ) {
    this.baseUrlInput = baseUrlInput;
    this.apiKeyInput = apiKeyInput;
    this.modelNameInput = modelNameInput;
    this.temperatureInput = temperatureInput;
    this.systemPromptInput = systemPromptInput;
    this.confirmBtn = confirmBtn;
    this.nameInput = nameInput;
    this.featureInput = featureInput;
    this.characterInput = characterInput;
    this.addressInput = addressInput;
    this.characteristicInput = characteristicInput;
    this.constraintInput = constraintInput;

    this.setupEventListeners();
  }

  /**
   * 设置事件监听
   */
  private setupEventListeners(): void {
    // 提示词模板字段变化时更新预览
    [
      this.nameInput,
      this.featureInput,
      this.characterInput,
      this.addressInput,
      this.characteristicInput,
      this.constraintInput,
    ].forEach((input) => {
      input.addEventListener("input", () => {
        this.updateSystemPromptPreview();
      });
    });

    // 确认按钮
    this.confirmBtn.addEventListener("click", async () => {
      if (!this.chatSettingsState) {
        return;
      }

      const system_prompt = this.buildSystemPrompt();

      this.chatSettingsState = {
        ...this.chatSettingsState,
        openai_base_url: this.baseUrlInput.value.trim(),
        openai_api_key: this.apiKeyInput.value.trim(),
        model_name: this.modelNameInput.value.trim(),
        temperature: Number.isFinite(Number(this.temperatureInput.value))
          ? Number(this.temperatureInput.value)
          : this.chatSettingsState.temperature,
        system_prompt,
        name: this.nameInput.value.trim() || undefined,
        feature: this.featureInput.value.trim() || undefined,
        character: this.characterInput.value.trim() || undefined,
        address: this.addressInput.value.trim() || undefined,
        characteristic: this.characteristicInput.value.trim() || undefined,
        constraint: this.constraintInput.value.trim() || undefined,
      };

      await window.desktopPetApi.updateChatSettings(this.chatSettingsState);
    });
  }

  /**
   * 根据模板字段拼装系统提示词
   */
  private buildSystemPrompt(): string {
    const name = this.nameInput.value.trim() || "日和";
    const feature = this.featureInput.value.trim() || "可爱";
    const character = this.characterInput.value.trim() || "AI少女";
    const address = this.addressInput.value.trim() || "主人";
    const characteristic = this.characteristicInput.value.trim();
    const constraint = this.constraintInput.value.trim();

    let prompt = `你是${name}，一个${feature}的${character}，称呼用户为${address}。`;
    if (characteristic) {
      prompt += `\n${characteristic}`;
    }
    if (constraint) {
      prompt += `\n${constraint}`;
    }
    return prompt;
  }

  /**
   * 更新系统提示词预览
   */
  private updateSystemPromptPreview(): void {
    this.systemPromptInput.value = this.buildSystemPrompt();
  }

  /**
   * 渲染 LLM 设置
   * @param state 聊天设置状态
   * @param forceUpdate 是否强制更新内部状态（模型切换时需要）
   */
  render(state: ChatSettingsState | null, forceUpdate = false): void {
    // 首次加载或强制更新时设置内部状态
    if (state && (!this.chatSettingsState || forceUpdate)) {
      this.chatSettingsState = state;
    }

    // 使用内部状态渲染
    if (!this.chatSettingsState) {
      return;
    }

    this.baseUrlInput.value = this.chatSettingsState.openai_base_url;
    this.apiKeyInput.value = this.chatSettingsState.openai_api_key;
    this.modelNameInput.value = this.chatSettingsState.model_name;
    this.temperatureInput.value = String(this.chatSettingsState.temperature);

    // 渲染提示词模板字段
    this.nameInput.value = this.chatSettingsState.name || "";
    this.featureInput.value = this.chatSettingsState.feature || "";
    this.characterInput.value = this.chatSettingsState.character || "";
    this.addressInput.value = this.chatSettingsState.address || "";
    this.characteristicInput.value = this.chatSettingsState.characteristic || "";
    this.constraintInput.value = this.chatSettingsState.constraint || "";

    // 更新系统提示词预览
    this.updateSystemPromptPreview();
  }

  /**
   * 获取聊天设置状态
   */
  getChatSettingsState(): ChatSettingsState | null {
    return this.chatSettingsState;
  }
}
