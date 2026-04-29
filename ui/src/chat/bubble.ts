/**
 * 气泡文本管理
 */

/**
 * 气泡管理器
 */
export class BubbleManager {
  private bubble: HTMLDivElement;

  constructor(bubble: HTMLDivElement) {
    this.bubble = bubble;
  }

  /**
   * 设置气泡文本
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
