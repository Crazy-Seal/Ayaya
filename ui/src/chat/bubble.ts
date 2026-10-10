/**
 * 气泡文本管理
 */

/**
 * 气泡管理器
 */
export class BubbleManager {
  private bubble: HTMLDivElement;

  /**
   * 绑定桌面回复气泡元素，初始化用于连续句子显示的状态。
   *
   * Args:
   *     bubble (HTMLDivElement): 显示最近一句回复的桌面气泡元素。
   */
  constructor(bubble: HTMLDivElement) {
    this.bubble = bubble;
  }

  /**
   * 设置气泡文本
   *
   * Args:
   *     text (string): 本次需要展示或输入的文本。
   */
  setText(text: string): void {
    this.bubble.textContent = text;
  }

  /**
   * 获取气泡文本
   */
  getText(): string | null {
    return this.bubble.textContent;
  }
}
