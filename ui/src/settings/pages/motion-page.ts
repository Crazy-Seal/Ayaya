import { PageEventType } from "../types.js";
import { MotionSettingType } from "../../../shared-types.js";
import { buildSystemPrompt } from "../utils/prompt-builder.js";

/**
 * 表情控制页面
 */

import type {
  MotionConfig,
  ISettingsPage,
  PageRenderData,
  PageEditingData,
  PageEventCallback,
  ChatSettingsState,
} from "../types.js";

/**
 * Model3.json 结构中的动作定义
 */
type Model3MotionGroup = Array<{ File: string }>;

type Model3FileReferences = {
  Moc?: string;
  Textures?: string[];
  Motions?: Record<string, Model3MotionGroup>;
};

type Model3Json = {
  Version: number;
  FileReferences: Model3FileReferences;
};

/**
 * 表情控制页面管理器（纯视图组件）
 */
export class MotionPage implements ISettingsPage {
  private motionTableBody: HTMLTableSectionElement;
  private motionEmpty: HTMLDivElement;
  private confirmBtn: HTMLButtonElement;
  private systemPromptPreview: HTMLTextAreaElement;

  private eventCallback?: PageEventCallback;

  // 当前渲染的动作列表（从 dependencies 获取）
  private availableMotions: string[] = [];

  // 已保存的状态（用于预览）
  private savedState: ChatSettingsState | null = null;

  /**
   * 绑定动作设置表格、空状态和提示词预览，后续按模型可用动作渲染。
   *
   * Args:
   *     motionTableBody (HTMLTableSectionElement): 显示动作配置行的表格正文。
   *     motionEmpty (HTMLDivElement): 没有动作时显示的占位元素。
   *     confirmBtn (HTMLButtonElement): 用户点击后提交本页修改的按钮。
   *     systemPromptPreview (HTMLTextAreaElement): 显示动作规则提示词的只读输入框。
   */
  constructor(
    motionTableBody: HTMLTableSectionElement,
    motionEmpty: HTMLDivElement,
    confirmBtn: HTMLButtonElement,
    systemPromptPreview: HTMLTextAreaElement
  ) {
    this.motionTableBody = motionTableBody;
    this.motionEmpty = motionEmpty;
    this.confirmBtn = confirmBtn;
    this.systemPromptPreview = systemPromptPreview;

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
    // 保存按钮
    this.confirmBtn.addEventListener("click", /**
     * 用户点击动作确认后通知设置控制器保存本页规则。
     */
    () => {
      this.eventCallback?.({ type: PageEventType.SUBMIT, page: "motion" });
    });

    // 表格事件委托
    this.motionTableBody.addEventListener("click", /**
     * 用户点击动作预览按钮时取得对应动作名并请求桌面播放。
     *
     * Args:
     *     event (PointerEvent): 调用方或浏览器触发的当前事件。
     */
    (event) => {
      const target = event.target as HTMLElement;
      const previewBtn = target.closest<HTMLButtonElement>(".motion-preview-btn");
      if (previewBtn) {
        const motionName = previewBtn.dataset.motionName;
        if (motionName) {
          this.previewMotion(motionName);
        }
      }
    });

    // 设置下拉框变化
    this.motionTableBody.addEventListener("change", /**
     * 用户改变动作类型后更新本行设置，并确保只有一项空闲动作。
     *
     * Args:
     *     event (Event): 调用方或浏览器触发的当前事件。
     */
    (event) => {
      const target = event.target as HTMLElement;
      if (target.classList.contains("motion-setting-select")) {
        const motionName = target.dataset.motionName;
        const value = (target as HTMLSelectElement).value as MotionSettingType;
        if (motionName) {
          this.handleSettingChange(motionName, value);
        }
      }
    });

    // 标签输入变化
    this.motionTableBody.addEventListener("input", /**
     * 用户编辑表情标签后更新本行动作标签及提示词预览。
     *
     * Args:
     *     event (Event): 调用方或浏览器触发的当前事件。
     */
    (event) => {
      const target = event.target as HTMLElement;
      if (target.classList.contains("motion-label-input")) {
        const motionName = target.dataset.motionName;
        const value = (target as HTMLInputElement).value;
        if (motionName) {
          this.handleLabelChange(motionName, value);
        }
      }
    });
  }

  /**
   * 渲染页面
   *
   * Args:
   *     data (PageRenderData): 当前已保存设置和本页所需模型、插件及工具目录。
   */
  render(data: PageRenderData): void {
    // 保存已保存状态
    this.savedState = data.saved;

    // 获取可用动作列表
    this.availableMotions = data.dependencies?.availableMotions || [];

    // 获取动作配置（只使用已保存状态）
    let motionConfigs: MotionConfig[];
    if (data.dependencies?.modelConfig) {
      const activeModel = data.dependencies.modelConfig.models.find(
        /**
         * 渲染动作配置时查找当前活动模型。
         *
         * Args:
         *     m ({ id: string; name: string; sessionId: string; source: "builtin" | "custom"; deletable: boolean; offsetX: number; offsetY: number; userScale: number; followCursor: boolean; motionConfig?: MotionConfig[] | undefined; entry?: string | undefined; modelUrl?: string | undefined; }): 当前模型动作的配置记录。
         *
         * Returns:
         *     boolean: 模型编号与活动编号一致时为 true。
         */
        (m) => m.id === data.dependencies!.modelConfig!.activeModelId
      );
      motionConfigs = activeModel?.motionConfig || this.createDefaultMotionConfigs();
    } else {
      motionConfigs = this.createDefaultMotionConfigs();
    }

    // 渲染表格
    this.renderMotionTable(motionConfigs);

    // 更新系统提示词预览
    this.updateSystemPromptPreview(data.saved, motionConfigs);
  }

  /**
   * 创建默认动作配置
   */
  private createDefaultMotionConfigs(): MotionConfig[] {
    return this.availableMotions.map(/**
     * 为未配置动作生成默认规则，名为 idle 的动作默认空闲，其余默认不自动播放。
     *
     * Args:
     *     name (string): 当前对象的名称。
     *
     * Returns:
     *     { motionName: string; setting: "idle" | "none"; }: 当前动作的初始规则。
     */
    (name) => ({
      motionName: name,
      setting: name.toLowerCase() === MotionSettingType.IDLE ? MotionSettingType.IDLE : MotionSettingType.NONE,
    }));
  }

  /**
   * 获取当前编辑数据
   */
  getEditingData(): PageEditingData {
    return {
      motion: {
        motionConfigs: this.collectMotionConfigs(),
      },
    };
  }

  /**
   * 收集表格中的动作配置
   */
  private collectMotionConfigs(): MotionConfig[] {
    const configs: MotionConfig[] = [];
    this.motionTableBody.querySelectorAll<HTMLSelectElement>(".motion-setting-select").forEach(/**
     * 逐行动作收集类型和可选表情标签，组成当前尚未保存的配置。
     *
     * Args:
     *     select (HTMLSelectElement): 当前动作配置的下拉选择框。
     */
    (select) => {
      const motionName = select.dataset.motionName;
      if (motionName) {
        const setting = select.value as MotionSettingType;
        const labelInput = this.motionTableBody.querySelector<HTMLInputElement>(
          `.motion-label-input[data-motion-name="${motionName}"]`
        );
        const label = labelInput?.value.trim() || undefined;

        configs.push({
          motionName,
          setting,
          label: setting === MotionSettingType.EXPRESSION ? label : undefined,
        });
      }
    });
    return configs;
  }

  /**
   * 处理设置变化
   *
   * Args:
   *     motionName (string): 需要播放或保存规则的模型动作名称。
   *     setting (MotionSettingType): 当前动作的自动播放类型。
   */
  private handleSettingChange(motionName: string, setting: MotionSettingType): void {
    // 如果设置为 idle，清除其他 idle
    if (setting === MotionSettingType.IDLE) {
      this.motionTableBody.querySelectorAll<HTMLSelectElement>(".motion-setting-select").forEach(/**
       * 用户指定新的空闲动作时取消其他行的空闲标记，避免多项同时作为默认动作。
       *
       * Args:
       *     select (HTMLSelectElement): 当前动作配置的下拉选择框。
       */
      (select) => {
        if (select.dataset.motionName !== motionName && select.value === MotionSettingType.IDLE) {
          select.value = MotionSettingType.NONE;
        }
      });
    }

    // 更新标签输入框状态
    const labelInput = this.motionTableBody.querySelector<HTMLInputElement>(
      `.motion-label-input[data-motion-name="${motionName}"]`
    );
    if (labelInput) {
      labelInput.disabled = setting !== MotionSettingType.EXPRESSION;
      labelInput.placeholder = setting === MotionSettingType.EXPRESSION ? "输入标签" : "-";
      if (setting !== MotionSettingType.EXPRESSION) {
        labelInput.value = "";
      }
    }
  }

  /**
   * 处理标签变化
   *
   * Args:
   *     motionName (string): 需要播放或保存规则的模型动作名称。
   *     label (string): 当前工具、表情或动作的显示名称。
   */
  private handleLabelChange(motionName: string, label: string): void {
    // 标签变化时更新预览
    const motionConfigs = this.collectMotionConfigs();
    // 使用已保存的状态更新预览
    if (this.savedState) {
      this.updateSystemPromptPreview(this.savedState, motionConfigs);
    }
  }

  /**
   * 预览动作
   *
   * Args:
   *     motionName (string): 需要播放或保存规则的模型动作名称。
   */
  private previewMotion(motionName: string): void {
    window.desktopPetApi.playMotion?.(motionName);
  }

  /**
   * 渲染动作表格
   *
   * Args:
   *     motionConfigs (MotionConfig[]): 当前模型动作规则列表。
   */
  private renderMotionTable(motionConfigs: MotionConfig[]): void {
    this.motionTableBody.innerHTML = "";

    if (this.availableMotions.length === 0) {
      this.motionEmpty.hidden = false;
      return;
    }

    this.motionEmpty.hidden = true;

    for (const motionName of this.availableMotions) {
      const config = motionConfigs.find(/**
       * 按动作名称查找当前行已有规则，未找到则由外层使用默认配置。
       *
       * Args:
       *     c (MotionConfig): 当前动作的配置项。
       *
       * Returns:
       *     boolean: 动作名称匹配时为 true。
       */
      (c) => c.motionName === motionName) || {
        motionName,
        setting: MotionSettingType.NONE,
      };

      const row = document.createElement("tr");

      // 动作名称
      const nameCell = document.createElement("td");
      nameCell.className = "col-name";
      nameCell.textContent = motionName;

      // 标签输入（仅 expression 类型可编辑）
      const labelCell = document.createElement("td");
      labelCell.className = "col-label";
      const labelInput = document.createElement("input");
      labelInput.type = "text";
      labelInput.className = "motion-label-input ui-control ui-control--small";
      labelInput.dataset.motionName = motionName;
      labelInput.value = config.label || "";
      labelInput.disabled = config.setting !== MotionSettingType.EXPRESSION;
      labelInput.placeholder = config.setting === MotionSettingType.EXPRESSION ? "输入标签" : "-";
      labelCell.appendChild(labelInput);

      // 预览按钮
      const previewCell = document.createElement("td");
      previewCell.className = "col-preview";
      const previewBtn = document.createElement("button");
      previewBtn.type = "button";
      previewBtn.className = "motion-preview-btn ui-button ui-button--secondary ui-button--small";
      previewBtn.dataset.motionName = motionName;
      previewBtn.textContent = "播放";
      previewCell.appendChild(previewBtn);

      // 设置下拉框
      const settingCell = document.createElement("td");
      settingCell.className = "col-setting";
      const select = document.createElement("select");
      select.className = "motion-setting-select ui-control ui-control--small ui-select";
      select.dataset.motionName = motionName;
      select.ariaLabel = `${motionName} 的动作设置`;

      const options = [
        { value: MotionSettingType.NONE, text: "无" },
        { value: MotionSettingType.IDLE, text: "空闲" },
        { value: MotionSettingType.EXPRESSION, text: "表情" },
      ];

      for (const opt of options) {
        const option = document.createElement("option");
        option.value = opt.value;
        option.textContent = opt.text;
        option.selected = config.setting === opt.value;
        select.appendChild(option);
      }

      settingCell.appendChild(select);

      row.appendChild(nameCell);
      row.appendChild(labelCell);
      row.appendChild(previewCell);
      row.appendChild(settingCell);
      this.motionTableBody.appendChild(row);
    }
  }

  /**
   * 更新系统提示词预览
   *
   * Args:
   *     saved (PageRenderData["saved"]): 当前会话已保存的服务启用及权限策略。
   *     motionConfigs (MotionConfig[]): 当前模型动作规则列表。
   */
  private updateSystemPromptPreview(saved: PageRenderData["saved"], motionConfigs: MotionConfig[]): void {
    // 1. 先提取当前模型允许输出的表情标签，再交给共享提示词构建器。
    const expressionLabels = motionConfigs
      .filter(/**
       * 仅保留已声明为表情且填写标签的动作，供提示词预览。
       *
       * Args:
       *     c (MotionConfig): 当前动作的配置项。
       *
       * Returns:
       *     string | false | undefined: 表情设置与非空标签都满足时为 true。
       */
      (c) => c.setting === MotionSettingType.EXPRESSION && c.label)
      .map(/**
       * 提取已筛选动作的表情标签。
       *
       * Args:
       *     c (MotionConfig): 当前动作的配置项。
       *
       * Returns:
       *     string: 当前表情名称。
       */
      (c) => c.label!);

    this.systemPromptPreview.value = buildSystemPrompt(saved, expressionLabels);
  }
}
