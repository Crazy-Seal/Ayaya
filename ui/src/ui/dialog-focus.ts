/** 对话框的业务优先级；退出确认优先于聊天审批，数值层级由共享 CSS 负责。 */
export const DIALOG_PRIORITY = { NORMAL: "normal", APPLICATION_EXIT: "exit" } as const;
export type DialogPriority = typeof DIALOG_PRIORITY[keyof typeof DIALOG_PRIORITY];

/** 一个已打开对话框的焦点状态；关闭时移出当前文档的对话框列表。 */
interface DialogEntry {
  overlay: HTMLElement;
  priority: DialogPriority;
  initialFocus: HTMLElement | null;
  lastFocus: HTMLElement | null;
  cancel: () => void;
}

/** 当前文档共用的模态状态，首个对话框打开前记录原页面焦点。 */
interface DocumentDialogs {
  document: Document;
  entries: DialogEntry[];
  returnFocus: HTMLElement | null;
}

const documents = new WeakMap<Document, DocumentDialogs>();

/**
 * 取得当前最上层对话框，同级取最近打开的对话框，退出确认始终优先。
 *
 * Args:
 *     state (DocumentDialogs): 当前文档的已打开对话框及原页面焦点。
 *
 * Returns:
 *     DialogEntry | undefined: 当前负责鼠标与键盘操作的对话框。
 */
const topDialog = (state: DocumentDialogs): DialogEntry | undefined => {
  let top: DialogEntry | undefined;
  for (const entry of state.entries) {
    if (entry.overlay.hidden) continue;
    if (!top || entry.priority === top.priority || entry.priority === DIALOG_PRIORITY.APPLICATION_EXIT) top = entry;
  }
  return top;
};

/**
 * 把焦点交给最上层对话框，退出锁定后不再恢复任何功能控件的焦点。
 *
 * Args:
 *     state (DocumentDialogs): 当前文档的对话框状态。
 *     entry (DialogEntry): 允许接收用户操作的最上层对话框。
 */
const focusDialog = (state: DocumentDialogs, entry: DialogEntry): void => {
  if (state.document.body.dataset.applicationShutdown === "true") return;
  const target = entry.lastFocus ?? entry.initialFocus;
  if (target?.isConnected && !target.closest("[hidden]") && !target.matches(":disabled")) target.focus();
  else entry.overlay.focus();
};

/**
 * 同步所有对话框的操作权限，底层对话框保持显示但不能点击、聚焦或接收键盘。
 *
 * Args:
 *     state (DocumentDialogs): 当前文档的对话框状态。
 */
const synchronizeDialogs = (state: DocumentDialogs): void => {
  const top = topDialog(state);
  for (const entry of state.entries) entry.overlay.inert = entry !== top;
  if (top && !top.overlay.contains(state.document.activeElement)) focusDialog(state, top);
};

/**
 * 为一个文档安装唯一的键盘和焦点监听，全部确认框与审批框共用它。
 *
 * Args:
 *     doc (Document): 对话框所属文档。
 *
 * Returns:
 *     DocumentDialogs: 当前文档唯一的模态状态。
 */
const documentDialogs = (doc: Document): DocumentDialogs => {
  const existing = documents.get(doc);
  if (existing) return existing;
  const state: DocumentDialogs = { document: doc, entries: [], returnFocus: null };
  documents.set(doc, state);
  doc.addEventListener("focusin", /**
   * 焦点变化后记录顶层对话框的当前控件，阻止底层审批或异步回调抢走焦点。
   *
   * Args:
   *     event (FocusEvent): 浏览器刚完成的焦点移动。
   */
  (event) => {
    const top = topDialog(state);
    if (!top || doc.body.dataset.applicationShutdown === "true") return;
    if (top.overlay.contains(event.target as Node)) top.lastFocus = event.target as HTMLElement;
    else { event.stopImmediatePropagation(); focusDialog(state, top); }
  }, true);
  doc.addEventListener("keydown", /**
   * 只把 Escape、Tab 和确认按键交给顶层对话框，不让被遮住的审批产生批准或拒绝。
   *
   * Args:
   *     event (KeyboardEvent): 当前文档捕获的键盘事件。
   */
  (event) => {
    const top = topDialog(state);
    if (!top || doc.body.dataset.applicationShutdown === "true") return;
    // 1. Escape 只取消用户看到的最上层对话框。
    if (event.key === "Escape") {
      event.preventDefault(); event.stopImmediatePropagation(); top.cancel(); return;
    }
    // 2. 意外位于底层的焦点先移回顶层，当前按键不能触发底层按钮。
    if (!top.overlay.contains(doc.activeElement)) {
      event.preventDefault(); event.stopImmediatePropagation(); focusDialog(state, top); return;
    }
    if (event.key !== "Tab") return;
    const controls = Array.from(top.overlay.querySelectorAll<HTMLElement>(
      'button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), [tabindex="0"]',
    )).filter(/**
     * 只允许焦点经过当前可见且未被其他对话框禁止操作的控件。
     *
     * Args:
     *     element (HTMLElement): 当前候选控件。
     *
     * Returns:
     *     boolean: 控件可见且可操作时为 true。
     */
    (element) => !element.closest("[hidden], [inert]") && element.getClientRects().length > 0);
    // 3. Tab 在顶层首尾控件之间循环；没有控件时保持在对话框本身。
    if (!controls.length) { event.preventDefault(); return; }
    const first = controls[0]; const last = controls[controls.length - 1];
    if (event.shiftKey && doc.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && doc.activeElement === last) { event.preventDefault(); first.focus(); }
  }, true);
  return state;
};

/**
 * 为模态框提供文档级焦点管理，确认退出、工具审批和其他确认框按同一优先级规则协调。
 *
 * Args:
 *     overlay (HTMLElement): 当前模态遮罩，调用方在 activate 前已显示、在 deactivate 前已隐藏。
 *     cancel (() => void): 用户按 Escape 时取消当前对话框的回调。
 *
 * Returns:
 *     { activate: (initialFocus?: HTMLElement, priority?: DialogPriority) => void; deactivate: () => void }: 打开和关闭时调用的焦点管理入口。
 */
export const createDialogFocus = (overlay: HTMLElement, cancel: () => void) => {
  const state = documentDialogs(overlay.ownerDocument);
  const entry: DialogEntry = { overlay, cancel, priority: DIALOG_PRIORITY.NORMAL, initialFocus: null, lastFocus: null };
  return {
    /**
     * 将已显示的对话框加入共用列表，低优先级审批到达时不改变退出确认的焦点。
     *
     * Args:
     *     initialFocus (HTMLElement | undefined): 首次操作控件，确认框通常为取消按钮。
     *     priority (DialogPriority): 当前确认的业务优先级，默认是普通确认或审批。
     */
    activate(initialFocus?: HTMLElement, priority: DialogPriority = DIALOG_PRIORITY.NORMAL): void {
      if (!state.entries.length) state.returnFocus = state.document.activeElement as HTMLElement | null;
      entry.priority = priority; entry.initialFocus = initialFocus ?? null; entry.lastFocus = null;
      overlay.dataset.modalPriority = priority; overlay.tabIndex = -1;
      if (!state.entries.includes(entry)) state.entries.push(entry);
      synchronizeDialogs(state);
    },
    /** 关闭后恢复下一层对话框；全部关闭时恢复原页面焦点，退出锁定后不恢复任何功能操作。 */
    deactivate(): void {
      const index = state.entries.indexOf(entry);
      if (index < 0) return;
      state.entries.splice(index, 1); overlay.inert = false; delete overlay.dataset.modalPriority;
      synchronizeDialogs(state);
      if (!state.entries.length) {
        if (state.document.body.dataset.applicationShutdown !== "true"
          && state.returnFocus?.isConnected && !state.returnFocus.closest("[hidden], [inert]")) state.returnFocus.focus();
        state.returnFocus = null;
      }
    },
  };
};
