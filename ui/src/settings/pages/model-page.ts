/**
 * 模型管理页面
 */

import type { ModelConfig, ImportPreview } from "../types.js";
import { resolveAiName } from "../../../ai-identity.js";
import { ConfirmDialog } from "../../ui/confirm-dialog.js";
import {
  appendErrorMessage,
  type SettingsNotifier,
} from "../components/settings-toast.js";

/**
 * 展示 Live2D 模型目录及各会话已保存的 AI 名字，管理模型导入、切换和显示参数。
 *
 * Attributes:
 *     aiNames (Map<string, string>): 按会话编号缓存的已保存 AI 名字。
 *     nameVersions (Map<string, number>): 各会话收到名字保存通知的次数，用于忽略较早发起的名字查询。
 *     configRevision (number): 模型目录查询次数，旧查询不能覆盖后来切换后的目录。
 */
export class ModelPage {
  private modelList: HTMLDivElement;
  private importPreview: HTMLDivElement;
  private previewName: HTMLDivElement;
  private previewType: HTMLDivElement;
  private previewEntry: HTMLDivElement;
  private confirmImportBtn: HTMLButtonElement;
  private cancelImportBtn: HTMLButtonElement;
  private importModelBtn: HTMLButtonElement;
  private offsetXSlider: HTMLInputElement;
  private offsetYSlider: HTMLInputElement;
  private offsetXValue: HTMLSpanElement;
  private offsetYValue: HTMLSpanElement;
  private followCursorCheckbox: HTMLInputElement;
  private confirmDialog: ConfirmDialog;

  private currentConfig: ModelConfig | null = null;
  private pendingImport: ImportPreview | null = null;
  private syncingSliders = false;
  private readonly aiNames = new Map<string, string>();
  private readonly nameVersions = new Map<string, number>();
  private configRevision = 0;

  /**
   * 绑定模型列表、导入预览和显示参数控件，接入提示与删除确认依赖。
   *
   * Args:
   *     modelList (HTMLDivElement): 显示已导入模型的列表容器。
   *     importPreview (HTMLDivElement): 导入前显示模型信息的容器。
   *     previewName (HTMLDivElement): 显示待导入模型名称的元素。
   *     previewType (HTMLDivElement): 显示待导入模型类型的元素。
   *     previewEntry (HTMLDivElement): 显示模型入口文件的元素。
   *     confirmImportBtn (HTMLButtonElement): 确认保存待导入模型的按钮。
   *     cancelImportBtn (HTMLButtonElement): 取消当前导入预览的按钮。
   *     importModelBtn (HTMLButtonElement): 选择待导入模型的按钮。
   *     offsetXSlider (HTMLInputElement): 调整模型水平偏移的滑块。
   *     offsetYSlider (HTMLInputElement): 调整模型垂直偏移的滑块。
   *     offsetXValue (HTMLSpanElement): 显示水平偏移数值的元素。
   *     offsetYValue (HTMLSpanElement): 显示垂直偏移数值的元素。
   *     followCursorCheckbox (HTMLInputElement): 启用模型目光跟随鼠标的选项。
   *     confirmDialog (ConfirmDialog): 删除或切换模型前显示确认提示的对象。
   *     notifier (SettingsNotifier): 设置保存成功或失败时显示提示的对象。
   */
  constructor(
    modelList: HTMLDivElement,
    importPreview: HTMLDivElement,
    previewName: HTMLDivElement,
    previewType: HTMLDivElement,
    previewEntry: HTMLDivElement,
    confirmImportBtn: HTMLButtonElement,
    cancelImportBtn: HTMLButtonElement,
    importModelBtn: HTMLButtonElement,
    offsetXSlider: HTMLInputElement,
    offsetYSlider: HTMLInputElement,
    offsetXValue: HTMLSpanElement,
    offsetYValue: HTMLSpanElement,
    followCursorCheckbox: HTMLInputElement,
    confirmDialog: ConfirmDialog,
    private readonly notifier: SettingsNotifier
  ) {
    this.modelList = modelList;
    this.importPreview = importPreview;
    this.previewName = previewName;
    this.previewType = previewType;
    this.previewEntry = previewEntry;
    this.confirmImportBtn = confirmImportBtn;
    this.cancelImportBtn = cancelImportBtn;
    this.importModelBtn = importModelBtn;
    this.offsetXSlider = offsetXSlider;
    this.offsetYSlider = offsetYSlider;
    this.offsetXValue = offsetXValue;
    this.offsetYValue = offsetYValue;
    this.followCursorCheckbox = followCursorCheckbox;
    this.confirmDialog = confirmDialog;

    this.setupEventListeners();
  }

  /**
   * 设置事件监听
   */
  private setupEventListeners(): void {
    const unsubscribeName = window.desktopPetApi.onAiNameChanged?.(/**
     * LLM 配置保存成功后更新对应会话的模型卡片；同时标记正在等待的旧名字查询已过期。
     *
     * Args:
     *     payload (AiNameChangedPayload): 主进程通知的已保存会话编号与 AI 名字。
     */
    (payload) => {
      this.aiNames.set(payload.sessionId, payload.name);
      this.nameVersions.set(payload.sessionId, (this.nameVersions.get(payload.sessionId) ?? 0) + 1);
      if (this.currentConfig) this.renderModelList(this.currentConfig);
    });
    window.addEventListener("beforeunload", /** 页面卸载时取消 AI 名字监听，避免关闭设置窗口后继续更新模型列表。 */
    () => unsubscribeName?.());

    // 导入模型按钮
    this.importModelBtn.addEventListener("click", /**
     * 用户选择导入目录后展示名称、类别和入口预览；取消选择则不改变当前模型。
     */
    async () => {
      const preview = await window.desktopPetApi.previewLive2DImport();
      if (!preview) {
        return;
      }

      this.pendingImport = preview;
      this.previewName.textContent = `模型名：${preview.suggestedName}`;
      this.previewType.textContent = "来源类型：文件夹";
      this.previewEntry.textContent = `识别入口：${preview.entryRelativePath}`;
      this.importPreview.hidden = false;
    });

    // 确认导入
    this.confirmImportBtn.addEventListener("click", /**
     * 用户确认导入预览后请求保存资源，完成后刷新模型目录并清理预览。
     */
    async () => {
      if (!this.pendingImport) {
        return;
      }

      await window.desktopPetApi.importLive2DModel({
        selectedPath: this.pendingImport.selectedPath,
        suggestedName: this.pendingImport.suggestedName,
      });
      this.clearImportPreview();
      await this.refreshAfterModelChanged();
    });

    // 取消导入
    this.cancelImportBtn.addEventListener("click", /**
     * 用户取消导入时清除尚未保存的预览信息。
     */
    () => {
      this.clearImportPreview();
    });

    // 模型列表点击
    this.modelList.addEventListener("click", /**
     * 模型列表点击时区分删除按钮与模型切换，删除先等待确认，切换交给主进程保存。
     *
     * Args:
     *     event (PointerEvent): 调用方或浏览器触发的当前事件。
     */
    async (event) => {
      const target = event.target as HTMLElement | null;
      const deleteBtn = target?.closest<HTMLButtonElement>(".delete-model-btn");
      if (deleteBtn) {
        const modelId = deleteBtn.dataset.modelId;
        if (!modelId || deleteBtn.disabled) {
          return;
        }

        const confirmed = await this.confirmDialog.open({
          title: "删除模型确认",
          message: "将删除该模型所有的配置文件和会话记忆，请谨慎操作",
          confirmText: "确认删除",
          cancelText: "取消",
          variant: "danger",
        });
        if (!confirmed) {
          return;
        }

        try {
          await window.desktopPetApi.deleteModel(modelId);
          await this.refreshAfterModelChanged();
          this.notifier.success("模型及配置删除成功");
        } catch (error) {
          this.notifier.error(appendErrorMessage("模型及配置删除失败", error));
        }
        return;
      }

      const item = target?.closest<HTMLButtonElement>(".model-item");
      if (!item) {
        return;
      }

      const modelId = item.dataset.modelId;
      if (!modelId || this.currentConfig?.activeModelId === modelId) {
        return;
      }

      await window.desktopPetApi.setActiveModel(modelId);
      await this.refreshAfterModelChanged();
    });

    // 滑块变化
    this.offsetXSlider.addEventListener("input", /**
     * 用户拖动显示参数滑块时提交当前缩放及偏移。
     */
    () => {
      void this.updateActiveModelTransform();
    });

    this.offsetYSlider.addEventListener("input", /**
     * 用户改变另一显示参数滑块时同步当前模型显示设置。
     */
    () => {
      void this.updateActiveModelTransform();
    });

    this.followCursorCheckbox.addEventListener("change", /**
     * 用户切换目光跟随开关时保存当前模型显示设置。
     */
    () => {
      void this.updateActiveModelTransform();
    });
  }

  /**
   * 清除导入预览
   */
  private clearImportPreview(): void {
    this.pendingImport = null;
    this.previewName.textContent = "";
    this.previewType.textContent = "";
    this.previewEntry.textContent = "";
    this.importPreview.hidden = true;
  }

  /**
   * 更新激活模型的变换
   */
  private async updateActiveModelTransform(): Promise<void> {
    if (!this.currentConfig || this.syncingSliders) {
      return;
    }

    const modelId = this.currentConfig.activeModelId;
    const offsetX = Number(this.offsetXSlider.value);
    const offsetY = Number(this.offsetYSlider.value);
    this.offsetXValue.textContent = `${offsetX}`;
    this.offsetYValue.textContent = `${offsetY}`;

    await window.desktopPetApi.updateModelTransform({
      modelId,
      offsetX,
      offsetY,
      followCursor: this.followCursorCheckbox.checked,
    });

    const target = this.currentConfig.models.find(/**
     * 按当前活动编号查找需要保存显示参数的模型。
     *
     * Args:
     *     item ({ id: string; name: string; sessionId: string; source: "builtin" | "custom"; deletable: boolean; offsetX: number; offsetY: number; userScale: number; followCursor: boolean; motionConfig?: MotionConfig[] | undefined; entry?: string | undefined; modelUrl?: string | undefined; }): 当前遍历的配置或目录条目。
     *
     * Returns:
     *     boolean: 模型编号匹配时为 true。
     */
    (item) => item.id === modelId);
    if (target) {
      target.offsetX = offsetX;
      target.offsetY = offsetY;
      target.followCursor = this.followCursorCheckbox.checked;
    }
  }

  /**
   * 渲染模型列表，主名称显示会话的 AI 名字，右上角显示 Live2D 资源名字。
   *
   * Args:
   *     config (ModelConfig): 当前服务、模型或动作配置。
   */
  renderModelList(config: ModelConfig): void {
    this.modelList.innerHTML = "";

    for (const model of config.models) {
      const row = document.createElement("div");
      row.className = "model-item-row";

      const button = document.createElement("button");
      button.type = "button";
      button.className = `model-item ui-surface ui-surface--compact ${model.id === config.activeModelId ? "active" : ""}`;
      button.dataset.modelId = model.id;
      // 1. AI 名字来自已保存 LLM 配置；所有名称按纯文本显示，避免用户输入被解释成 HTML。
      const heading = document.createElement("div");
      heading.className = "model-heading";
      const aiName = document.createElement("span");
      aiName.className = "model-name";
      aiName.textContent = this.aiNames.get(model.sessionId) ?? "AI";
      const modelName = document.createElement("span");
      modelName.className = "model-resource-name";
      modelName.textContent = model.name;
      heading.append(aiName, modelName);
      const session = document.createElement("div");
      session.className = "model-session";
      session.textContent = `session_id: ${model.sessionId}`;
      button.append(heading, session);

      const deleteButton = document.createElement("button");
      deleteButton.type = "button";
      deleteButton.className = "delete-model-btn ui-button ui-button--danger ui-button--medium";
      deleteButton.dataset.modelId = model.id;
      deleteButton.textContent = "删除";
      deleteButton.disabled = !model.deletable;

      row.appendChild(button);
      row.appendChild(deleteButton);
      this.modelList.appendChild(row);
    }
  }

  /**
   * 渲染变换滑块
   *
   * Args:
   *     config (ModelConfig): 当前服务、模型或动作配置。
   */
  renderTransformSliders(config: ModelConfig): void {
    const active = config.models.find(/**
     * 渲染显示参数控件前查找配置中的活动模型。
     *
     * Args:
     *     item ({ id: string; name: string; sessionId: string; source: "builtin" | "custom"; deletable: boolean; offsetX: number; offsetY: number; userScale: number; followCursor: boolean; motionConfig?: MotionConfig[] | undefined; entry?: string | undefined; modelUrl?: string | undefined; }): 当前遍历的配置或目录条目。
     *
     * Returns:
     *     boolean: 当前模型为活动模型时为 true。
     */
    (item) => item.id === config.activeModelId);
    const offsetX = active?.offsetX ?? 0;
    const offsetY = active?.offsetY ?? 0;
    const followCursor = active?.followCursor ?? true;

    this.syncingSliders = true;
    this.offsetXSlider.value = String(Math.round(offsetX));
    this.offsetYSlider.value = String(Math.round(offsetY));
    this.offsetXValue.textContent = `${Math.round(offsetX)}`;
    this.offsetYValue.textContent = `${Math.round(offsetY)}`;
    this.followCursorCheckbox.checked = followCursor;
    this.syncingSliders = false;
  }

  /**
   * 先显示本地模型目录，再读取各会话的 AI 名字；名字查询失败时保留可操作的模型列表。
   *
   * Returns:
   *     Promise<ModelConfig>: 本次读取的模型目录；过期查询不会覆盖当前显示。
   */
  async refreshModelConfig(): Promise<ModelConfig> {
    // 1. 每次切换或刷新记录查询次数，较早返回的目录不覆盖后来发起的查询。
    const revision = ++this.configRevision;
    const config = await window.desktopPetApi.getModelConfig();
    if (revision !== this.configRevision) return config;
    this.currentConfig = config;
    this.renderModelList(config);
    this.renderTransformSliders(config);

    // 2. 名字独立加载，等待期间仍能切换或删除模型；新保存的名字优先于旧查询结果。
    const versions = new Map(this.nameVersions);
    try {
      const names = await window.desktopPetApi.getAiNames();
      if (revision !== this.configRevision) return config;
      for (const [sessionId, name] of Object.entries(names)) {
        if (versions.get(sessionId) === this.nameVersions.get(sessionId)) {
          this.aiNames.set(sessionId, resolveAiName(name));
        }
      }
      this.renderModelList(config);
    } catch (error) {
      if (revision === this.configRevision) {
        this.notifier.error(appendErrorMessage("读取 AI 名字失败", error));
      }
    }
    return config;
  }

  /**
   * 模型变化后刷新
   */
  async refreshAfterModelChanged(): Promise<void> {
    await this.refreshModelConfig();
  }

  /**
   * 获取当前配置
   */
  getCurrentConfig(): ModelConfig | null {
    return this.currentConfig;
  }

  /**
   * 更新配置中的变换数据
   *
   * Args:
   *     data ({ id: string; offsetX: number; offsetY: number; userScale: number; followCursor: boolean }): 调用方提供的接口或页面数据。
   */
  updateTransformData(data: { id: string; offsetX: number; offsetY: number; userScale: number; followCursor: boolean }): void {
    if (!this.currentConfig || data.id !== this.currentConfig.activeModelId) {
      return;
    }

    const target = this.currentConfig.models.find(/**
     * 显示参数通知到达后查找对应模型记录并更新页面缓存。
     *
     * Args:
     *     item ({ id: string; name: string; sessionId: string; source: "builtin" | "custom"; deletable: boolean; offsetX: number; offsetY: number; userScale: number; followCursor: boolean; motionConfig?: MotionConfig[] | undefined; entry?: string | undefined; modelUrl?: string | undefined; }): 当前遍历的配置或目录条目。
     *
     * Returns:
     *     boolean: 当前模型编号等于通知编号时为 true。
     */
    (item) => item.id === data.id);
    if (!target) {
      return;
    }

    target.offsetX = data.offsetX;
    target.offsetY = data.offsetY;
    target.userScale = data.userScale;
    target.followCursor = data.followCursor;
    this.renderTransformSliders(this.currentConfig);
  }
}
