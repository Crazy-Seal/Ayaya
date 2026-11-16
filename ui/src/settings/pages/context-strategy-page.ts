import { PageEventType } from "../types.js";
import type {
  ChatSettingsState,
  ISettingsPage,
  PageEditingData,
  PageEventCallback,
  PageRenderData,
} from "../types.js";

type ContextStrategyKey = keyof ChatSettingsState["context_strategy"];

export const CONTEXT_STRATEGY_FIELDS: ContextStrategyKey[] = [
  "compression_trigger_tokens_k",
];

/**
 * 提供上下文压缩阈值的数字输入下限。
 *
 * Args:
 *     key (ContextStrategyKey): 当前配置字段或索引名称。
 *
 * Returns:
 *     number: 输入框应使用的最小数值。
 */
export const getContextStrategyMinimum = (key: ContextStrategyKey): number => (
  10
);

/**
 * 展示和收集会话的压缩阈值，保存由设置页主控制器完成。
 */
export class ContextStrategyPage implements ISettingsPage {
  private inputs: Record<ContextStrategyKey, HTMLInputElement>;
  private eventCallback?: PageEventCallback;

  /**
   * 绑定数字输入框和确认按钮，提交时通知设置页主控制器。
   *
   * Args:
   *     inputs (Record<ContextStrategyKey, HTMLInputElement>): 上下文压缩设置对应的数字输入框集合。
   *     confirmBtn (HTMLButtonElement): 用户点击后提交本页修改的按钮。
   */
  constructor(inputs: Record<ContextStrategyKey, HTMLInputElement>, confirmBtn: HTMLButtonElement) {
    this.inputs = inputs;
    confirmBtn.addEventListener("click", /**
     * 用户点击确认后通知设置控制器保存当前上下文策略，不在页面组件内直接写后端。
     */
    () => {
      this.eventCallback?.({ type: PageEventType.SUBMIT, page: "contextStrategy" });
    });
  }

  /**
   * 登记设置页主控制器提供的提交事件接收函数。
   *
   * Args:
   *     callback (PageEventCallback): 设置页主控制器接收本页提交或编辑事件的回调。
   */
  onEvent(callback: PageEventCallback): void {
    this.eventCallback = callback;
  }

  /**
   * 将已保存上下文策略填入数字输入框。
   *
   * Args:
   *     data (PageRenderData): 当前已保存设置和本页所需模型、插件及工具目录。
   */
  render(data: PageRenderData): void {
    for (const key of CONTEXT_STRATEGY_FIELDS) {
      this.inputs[key].value = data.saved.context_strategy[key] == null
        ? ""
        : String(data.saved.context_strategy[key]);
    }
  }

  /**
   * 从输入框收集用户正在编辑的上下文策略，尚未保存到后端。
   *
   * Returns:
   *     PageEditingData: 本页编辑中的上下文压缩配置。
   */
  getEditingData(): PageEditingData {
    return {
      contextStrategy: {
        context_strategy: {
          compression_trigger_tokens_k: Number(this.inputs.compression_trigger_tokens_k.value),
        },
      },
    };
  }
}
