/**
 * 提供真实展示类所需的最小 DOM 行为，文本可从公开容器读取。
 *
 * Attributes:
 *     children (PresentationElement[]): 当前子节点。
 *     parent (PresentationElement | null): 用于移除提示及草稿的父节点。
 *     value (string): 节点自己的正文。
 */
export class PresentationElement {
  children: PresentationElement[] = [];
  parent: PresentationElement | null = null;
  private value = "";
  className = "";
  hidden = true;
  disabled = false;
  title = "";
  src = "";
  style: Partial<CSSStyleDeclaration> = {};
  attributes = new Map<string, string>();
  handlers = new Map<string, () => void>();
  scrollHeight = 100;
  classList = {
    /**
     *  提供展示器切换动画样式所需的占位方法。
     */
    add(): void {},
    /**
     *  提供展示器恢复动画样式所需的占位方法。
     */
    remove(): void {},
  };

  /**
   * 返回本节点与子节点的实际可见文字。
   *
   * Returns:
   *     string: 实际可见正文。
   */
  get textContent(): string {
    return this.value + this.children.map(/**
     * 由集合的 map 遍历每项数据时调用，读取 child.textContent 供当前步骤继续处理。
     *
     * 此定义用于：返回本节点与子节点的实际可见文字。
     *
     * Args:
     *     child (PresentationElement): 本次管理的子进程。
     */
    (child) => child.textContent).join("");
  }

  /**
   * 按 DOM 语义替换节点文字及子节点。
   *
   * Args:
   *     text (string): 新正文。
   */
  set textContent(text: string) {
    this.value = text;
    this.children = [];
  }

  /**
   * 清空历史重绘的容器。
   *
   * Args:
   *     _html (string): 展示器设置的 HTML，测试仅需要清空行为。
   */
  set innerHTML(_html: string) {
    this.value = "";
    this.children = [];
  }

  /**
   * 追加子节点并保存移除关系。
   *
   * Args:
   *     child (PresentationElement): 新节点。
   *
   * Returns:
   *     PresentationElement: 已追加节点。
   */
  appendChild(child: PresentationElement): PresentationElement {
    // 1. 与真实 DOM 一致，追加已有节点表示移动，不能复制状态提示。
    child.remove();
    child.parent = this;
    this.children.push(child);
    return child;
  }

  /**
   *  从父节点移除提示或草稿。
   */
  remove(): void {
    if (this.parent) this.parent.children.splice(this.parent.children.indexOf(this), 1);
  }

  /**
   * 返回空图片集合，当前测试只检查文字。
   *
   * Returns:
   *     PresentationElement[]: 没有图片的查询结果。
   */
  querySelectorAll(): PresentationElement[] { return []; }

  /**
   * 记录公开 DOM 属性，供测试核对控件的无障碍名称。
   * Args:
   *     name (string): DOM 属性名称。
   *     value (string): 属性值。
   */
  setAttribute(name: string, value: string): void { this.attributes.set(name, value); }

  /**
   *  提供展示器滚动容器所需的占位方法。
   */
  scrollTo(): void {}

  /**
   * 登记公开控件回调，测试可模拟真实点击。
   *
   * Args:
   *     kind (string): DOM 事件类别。
   *     callback (() => void): 当前控件处理函数。
   */
  addEventListener(kind: string, callback: () => void): void { this.handlers.set(kind, callback); }

  /**
   * 按真实 DOM 顺序追加任务行中的节点。
   *
   * Args:
   *     children (PresentationElement[]): 当前任务行的节点。
   */
  append(...children: PresentationElement[]): void { for (const child of children) this.appendChild(child); }

  /**
   *  清空任务列表，让刷新后的记录替换旧行。
   */
  replaceChildren(): void { this.children = []; this.value = ""; }
}
