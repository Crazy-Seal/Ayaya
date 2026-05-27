/**
 * 设置窗口入口
 */

import "../settings.css";

import { ConfirmDialog } from "./components/confirm-dialog.js";
import { ModelPage } from "./pages/model-page.js";
import { LlmPage } from "./pages/llm-page.js";
import { ToolsPage } from "./pages/tools-page.js";
import { MultimodalPage } from "./pages/multimodal-page.js";
import type { ChatSettingsState } from "./types.js";

/**
 * 设置窗口应用
 */
class SettingsApp {
  private sidebar: HTMLDivElement;
  private minBtn: HTMLButtonElement;
  private closeBtn: HTMLButtonElement;

  private modelPage: ModelPage;
  private llmPage: LlmPage;
  private toolsPage: ToolsPage;
  private multimodalPage: MultimodalPage;
  private confirmDialog: ConfirmDialog;

  private currentPage = "model";
  private chatSettingsState: ChatSettingsState | null = null;

  constructor() {
    // 获取 DOM 元素
    this.sidebar = this.getElement("#settings-sidebar");
    this.minBtn = this.getElement("#settings-min-btn");
    this.closeBtn = this.getElement("#settings-close-btn");

    // 初始化确认对话框
    this.confirmDialog = new ConfirmDialog(
      this.getElement("#delete-confirm-dialog"),
      this.getElement("#delete-confirm-cancel"),
      this.getElement("#delete-confirm-ok")
    );

    // 初始化模型管理页面
    this.modelPage = new ModelPage(
      this.getElement("#model-list"),
      this.getElement("#import-preview"),
      this.getElement("#preview-name"),
      this.getElement("#preview-type"),
      this.getElement("#preview-entry"),
      this.getElement("#confirm-import-btn"),
      this.getElement("#cancel-import-btn"),
      this.getElement("#import-model-btn"),
      this.getElement("#slider-offset-x"),
      this.getElement("#slider-offset-y"),
      this.getElement("#slider-offset-x-value"),
      this.getElement("#slider-offset-y-value"),
      this.getElement("#checkbox-follow-cursor"),
      this.confirmDialog
    );

    // 初始化 LLM 配置页面
    this.llmPage = new LlmPage(
      this.getElement("#llm-base-url"),
      this.getElement("#llm-api-key"),
      this.getElement("#llm-model-name"),
      this.getElement("#llm-temperature"),
      this.getElement("#llm-system-prompt"),
      this.getElement("#llm-confirm-btn"),
      this.getElement("#llm-name"),
      this.getElement("#llm-feature"),
      this.getElement("#llm-character"),
      this.getElement("#llm-address"),
      this.getElement("#llm-characteristic"),
      this.getElement("#llm-constraint")
    );

    // 初始化工具配置页面
    this.toolsPage = new ToolsPage(
      this.getElement("#tools-table-body"),
      this.getElement("#tools-empty"),
      this.getElement("#tools-confirm-btn")
    );

    // 初始化多模态配置页面
    this.multimodalPage = new MultimodalPage(
      this.getElement("#checkbox-hide-on-screenshot")
    );

    this.setupEventListeners();
  }

  /**
   * 获取 DOM 元素
   */
  private getElement<T extends HTMLElement>(selector: string): T {
    const element = document.querySelector<T>(selector);
    if (!element) {
      throw new Error(`设置窗口初始化失败：找不到元素 ${selector}`);
    }
    return element;
  }

  /**
   * 设置事件监听
   */
  private setupEventListeners(): void {
    // 侧边栏导航
    this.sidebar.addEventListener("click", (event) => {
      const target = event.target as HTMLElement | null;
      const tab = target?.closest<HTMLButtonElement>(".settings-tab");
      if (!tab) {
        return;
      }

      const page = tab.dataset.page;
      if (!page) {
        return;
      }

      this.currentPage = page;

      document.querySelectorAll<HTMLButtonElement>(".settings-tab").forEach((item) => {
        item.classList.toggle("active", item === tab);
      });

      document.querySelectorAll<HTMLDivElement>(".settings-page").forEach((item) => {
        item.classList.toggle("active", item.dataset.page === page);
      });

      this.applyPageEnterRender(page);
    });

    // 最小化按钮
    this.minBtn.addEventListener("click", () => {
      window.desktopPetApi.minimizeCurrentWindow();
    });

    // 关闭按钮
    this.closeBtn.addEventListener("click", () => {
      window.desktopPetApi.closeCurrentWindow();
    });

    // 模型变化监听
    const unsubscribeModelChanged = window.desktopPetApi.onModelChanged?.(() => {
      void this.refreshAfterModelChanged();
    });

    // 模型变换监听
    const unsubscribeTransformChanged = window.desktopPetApi.onModelTransformChanged?.((payload) => {
      this.modelPage.updateTransformData(payload);
    });

    // 页面卸载清理
    window.addEventListener("beforeunload", () => {
      if (typeof unsubscribeTransformChanged === "function") {
        unsubscribeTransformChanged();
      }
      if (typeof unsubscribeModelChanged === "function") {
        unsubscribeModelChanged();
      }
    });
  }

  /**
   * 应用页面进入渲染
   * @param forceUpdate 是否强制更新页面内部状态（模型切换时需要）
   */
  private applyPageEnterRender(page: string, forceUpdate = false): void {
    if (page === "llm") {
      this.llmPage.render(this.chatSettingsState, forceUpdate);
      return;
    }

    if (page === "tools") {
      this.toolsPage.render(this.chatSettingsState, forceUpdate);
      return;
    }

    if (page === "multimodal") {
      void this.multimodalPage.render();
    }
  }

  /**
   * 初始化聊天设置
   */
  private async initChatSettings(): Promise<void> {
    this.chatSettingsState = await window.desktopPetApi.getChatSettings();
  }

  /**
   * 模型变化后刷新
   */
  private async refreshAfterModelChanged(): Promise<void> {
    await Promise.all([this.modelPage.refreshModelConfig(), this.initChatSettings()]);
    // 模型变化后强制更新所有页面的内部状态
    this.llmPage.render(this.chatSettingsState, true);
    this.toolsPage.render(this.chatSettingsState, true);
    // 当前页面重新渲染
    this.applyPageEnterRender(this.currentPage, true);
  }

  /**
   * 初始化
   */
  async init(): Promise<void> {
    await Promise.all([
      this.toolsPage.refreshTools(),
      this.modelPage.refreshModelConfig(),
      this.initChatSettings(),
      this.multimodalPage.render(),
    ]);

    this.applyPageEnterRender(this.currentPage);
  }
}

// 启动应用
void new SettingsApp().init();
