import { PageEventType } from "../types.js";
import { DEFAULT_AI_NAME } from "../../../ai-identity.js";
import { buildSystemPrompt } from "../utils/prompt-builder.js";

/**
 * LLM 配置页面
 */

import type {
  ISettingsPage,
  PageRenderData,
  PageEditingData,
  PageEventCallback,
} from "../types.js";
import { bindPressToRevealSecret } from "../components/press-to-reveal-secret.js";

/**
 * LLM 配置页面管理器（纯视图组件）
 */
export class LlmPage implements ISettingsPage {
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

  private eventCallback?: PageEventCallback;

  /** 表情标签列表（从 motion 配置获取） */
  private expressionLabels: string[] = [];

  /**
   * 绑定模型、凭据和角色输入框，接入确认按钮及按住显示凭据行为。
   *
   * Args:
   *     baseUrlInput (HTMLInputElement): 编辑模型服务地址的输入框。
   *     apiKeyInput (HTMLInputElement): 编辑模型 API 凭据的输入框。
   *     apiKeyRevealButton (HTMLButtonElement): 按住时临时显示凭据的按钮。
   *     modelNameInput (HTMLInputElement): 编辑模型名称的输入框。
   *     temperatureInput (HTMLInputElement): 编辑模型生成温度的输入框。
   *     systemPromptInput (HTMLTextAreaElement): 显示或编辑系统提示词的输入框。
   *     confirmBtn (HTMLButtonElement): 用户点击后提交本页修改的按钮。
   *     nameInput (HTMLInputElement): 编辑助手称呼的输入框。
   *     featureInput (HTMLInputElement): 编辑助手特征的输入框。
   *     characterInput (HTMLInputElement): 编辑助手角色的输入框。
   *     addressInput (HTMLInputElement): 编辑助手对用户称呼的输入框。
   *     characteristicInput (HTMLTextAreaElement): 编辑助手性格说明的输入框。
   *     constraintInput (HTMLTextAreaElement): 编辑助手行为约束的输入框。
   */
  constructor(
    baseUrlInput: HTMLInputElement,
    apiKeyInput: HTMLInputElement,
    apiKeyRevealButton: HTMLButtonElement,
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
    bindPressToRevealSecret(this.apiKeyInput, apiKeyRevealButton);
    this.modelNameInput = modelNameInput;
    this.temperatureInput = temperatureInput;
    this.systemPromptInput = systemPromptInput;
    this.confirmBtn = confirmBtn;
    this.nameInput = nameInput;
    this.nameInput.placeholder = DEFAULT_AI_NAME;
    this.featureInput = featureInput;
    this.characterInput = characterInput;
    this.addressInput = addressInput;
    this.characteristicInput = characteristicInput;
    this.constraintInput = constraintInput;

    this.setupEventListeners();
  }

  /**
   * 设置事件回调
   *
   * Args:
   *     callback (PageEventCallback): 设置页主控制器接收本页提交或编辑事件的回调。
   */
  onEvent(callback: PageEventCallback): void {
    this.eventCallback = callback;
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
    ].forEach(/**
     * 为每个角色相关输入框登记变化监听，修改后更新系统提示词预览。
     *
     * Args:
     *     input (HTMLTextAreaElement | HTMLInputElement): 当前日志输入或配置输入框。
     */
    (input) => {
      input.addEventListener("input", /**
       * 角色字段输入变化后重新生成当前提示词预览。
       */
      () => {
        this.updateSystemPromptPreview();
      });
    });

    // 确认按钮
    this.confirmBtn.addEventListener("click", /**
     * 用户点击确认后通知设置控制器校验并保存模型和角色设置。
     */
    () => {
      this.eventCallback?.({ type: PageEventType.SUBMIT, page: "llm" });
    });
  }

  /**
   * 渲染页面
   *
   * Args:
   *     data (PageRenderData): 当前已保存设置和本页所需模型、插件及工具目录。
   */
  render(data: PageRenderData): void {
    // 保存表情标签列表
    this.expressionLabels = data.dependencies?.expressionLabels || [];

    // 只使用已保存状态
    const llmData = {
      openai_base_url: data.saved.openai_base_url,
      openai_api_key: data.saved.openai_api_key,
      model_name: data.saved.model_name,
      temperature: data.saved.temperature,
      name: data.saved.name || "",
      feature: data.saved.feature || "",
      character: data.saved.character || "",
      address: data.saved.address || "",
      characteristic: data.saved.characteristic || "",
      constraint: data.saved.constraint || "",
    };

    // 渲染表单
    this.baseUrlInput.value = llmData.openai_base_url;
    this.apiKeyInput.value = llmData.openai_api_key;
    this.modelNameInput.value = llmData.model_name;
    this.temperatureInput.value = String(llmData.temperature);

    // 渲染提示词模板字段
    this.nameInput.value = llmData.name;
    this.featureInput.value = llmData.feature;
    this.characterInput.value = llmData.character;
    this.addressInput.value = llmData.address;
    this.characteristicInput.value = llmData.characteristic;
    this.constraintInput.value = llmData.constraint;

    // 更新系统提示词预览
    this.updateSystemPromptPreview();
  }

  /**
   * 获取当前编辑数据
   */
  getEditingData(): PageEditingData {
    return {
      llm: {
        openai_base_url: this.baseUrlInput.value.trim(),
        openai_api_key: this.apiKeyInput.value.trim(),
        model_name: this.modelNameInput.value.trim(),
        temperature: Number.isFinite(Number(this.temperatureInput.value))
          ? Number(this.temperatureInput.value)
          : 0.7,
        name: this.nameInput.value.trim(),
        feature: this.featureInput.value.trim(),
        character: this.characterInput.value.trim(),
        address: this.addressInput.value.trim(),
        characteristic: this.characteristicInput.value.trim(),
        constraint: this.constraintInput.value.trim(),
      },
    };
  }

  /**
   * 根据模板字段拼装系统提示词
   */
  private buildSystemPrompt(): string {
    return buildSystemPrompt({
      name: this.nameInput.value.trim(),
      feature: this.featureInput.value.trim(),
      character: this.characterInput.value.trim(),
      address: this.addressInput.value.trim(),
      characteristic: this.characteristicInput.value.trim(),
      constraint: this.constraintInput.value.trim(),
    }, this.expressionLabels);
  }

  /**
   * 更新系统提示词预览
   */
  private updateSystemPromptPreview(): void {
    this.systemPromptInput.value = this.buildSystemPrompt();
  }
}
