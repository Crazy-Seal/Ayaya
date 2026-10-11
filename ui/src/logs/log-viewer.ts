import { initializeIcons } from "../ui/icons.js";
import { setupWindowGestures } from "../ui/window-gestures.js";
import { setupApplicationShutdownUi } from "../ui/application-shutdown.js";
import "./logs.css";

import type {
  LogLevel,
  LogRecord,
  LogSide,
  LogSource,
} from "../../shared-types.js";
import {
  LogViewerModel,
  type LogFilter,
} from "./log-viewer-model.js";
import {
  LogViewportState,
  resolveAnchoredScrollTop,
  selectRestorationAnchorId,
} from "./log-viewport.js";

/**
 * 初始化日志页面时查找必须存在的控件，缺失则明确报错。
 *
 * Args:
 *     selector (string): 必须存在的页面元素选择器。
 *
 * Returns:
 *     T: 符合请求类型的页面元素。
 */
const getElement = <T extends HTMLElement>(selector: string): T => {
  const element = document.querySelector<T>(selector);
  if (!element) throw new Error(`日志窗口缺少元素: ${selector}`);
  return element;
};

/**
 * 保存一类日志的列表、数量、筛选和清空控件。
 */
interface SideElements {
  list: HTMLDivElement;
  count: HTMLDivElement;
  level: HTMLSelectElement;
  source: HTMLSelectElement;
  autoscroll: HTMLInputElement;
  clear: HTMLButtonElement;
}

/**
 * 保存一类日志的跟随状态、已渲染编号以及滚动定时器。
 */
interface SideViewState {
  viewport: LogViewportState;
  renderedIds: Set<number>;
  matchingCount: number;
  bottomFrame: number | null;
  userIntentTimer: number | null;
}

/**
 * 记录列表更新前首条可见日志编号及其位置，供更新后保持阅读位置。
 */
interface ViewAnchor {
  recordId: number;
  offset: number;
  scrollTop: number;
}

const elements: Record<LogSide, SideElements> = {
  frontend: {
    list: getElement("#frontend-logs"),
    count: getElement("#frontend-count"),
    level: getElement("#frontend-level"),
    source: getElement("#frontend-source"),
    autoscroll: getElement("#frontend-autoscroll"),
    clear: getElement("#frontend-clear"),
  },
  backend: {
    list: getElement("#backend-logs"),
    count: getElement("#backend-count"),
    level: getElement("#backend-level"),
    source: getElement("#backend-source"),
    autoscroll: getElement("#backend-autoscroll"),
    clear: getElement("#backend-clear"),
  },
};

const viewStates: Record<LogSide, SideViewState> = {
  frontend: {
    viewport: new LogViewportState(elements.frontend.autoscroll.checked),
    renderedIds: new Set(),
    matchingCount: 0,
    bottomFrame: null,
    userIntentTimer: null,
  },
  backend: {
    viewport: new LogViewportState(elements.backend.autoscroll.checked),
    renderedIds: new Set(),
    matchingCount: 0,
    bottomFrame: null,
    userIntentTimer: null,
  },
};

const search = getElement<HTMLInputElement>("#log-search");
const pauseButton = getElement<HTMLButtonElement>("#log-pause-btn");
const status = getElement<HTMLDivElement>("#log-status");
const resizeObservers: ResizeObserver[] = [];
let model: LogViewerModel | null = null;
const MAX_RENDERED_ROWS = 1_000;
const USER_SCROLL_KEYS = new Set([
  "ArrowUp",
  "ArrowDown",
  "PageUp",
  "PageDown",
  "Home",
  "End",
  " ",
]);

/**
 * 把来源下拉框选择转换为过滤集合，空选择表示全部来源。
 *
 * Args:
 *     value (string): 当前待检查或转换的值。
 *
 * Returns:
 *     Set<LogSource>: 选中的来源集合，空值对应空集合。
 */
const selectedSourceSet = (value: string): Set<LogSource> =>
  value ? new Set([value as LogSource]) : new Set<LogSource>();

/**
 * 从日志页面控件读取当前类别的筛选条件。
 *
 * Args:
 *     side (LogSide): 前端或后端日志类别。
 *
 * Returns:
 *     LogFilter: 关键词、最低级别和来源组合。
 */
const getFilter = (side: LogSide): LogFilter => ({
  query: search.value,
  minimumLevel: elements[side].level.value as LogLevel,
  sources: selectedSourceSet(elements[side].source.value),
});

/**
 * 创建日志列表末尾的隐藏定位元素，供底部布局和空状态插入使用。
 *
 * Returns:
 *     HTMLDivElement: 新的底部定位元素。
 */
const createBottomAnchor = (): HTMLDivElement => {
  const anchor = document.createElement("div");
  anchor.className = "log-bottom-anchor";
  anchor.setAttribute("aria-hidden", "true");
  return anchor;
};

/**
 * 复用指定类别列表的底部定位元素，没有时创建。
 *
 * Args:
 *     side (LogSide): 前端或后端日志类别。
 *
 * Returns:
 *     HTMLDivElement: 已放入列表的底部元素。
 */
const getBottomAnchor = (side: LogSide): HTMLDivElement => {
  const list = elements[side].list;
  const existing = list.querySelector<HTMLDivElement>(".log-bottom-anchor");
  if (existing) return existing;
  const anchor = createBottomAnchor();
  list.append(anchor);
  return anchor;
};

/**
 * 仅在自动跟随启用时滚到末尾，并在下一帧布局完成后再次对齐。
 *
 * Args:
 *     side (LogSide): 前端或后端日志类别。
 */
const scrollToBottom = (side: LogSide): void => {
  const sideElements = elements[side];
  const state = viewStates[side];
  if (!state.viewport.isFollowing()) return;

  /**
   * 当前类别仍启用跟随时应用底部位置，用户取消跟随后不强制滚动。
   */
  const applyBottom = (): void => {
    if (!state.viewport.isFollowing()) return;
    getBottomAnchor(side);
    sideElements.list.scrollTop = sideElements.list.scrollHeight;
  };

  applyBottom();
  if (state.bottomFrame !== null) cancelAnimationFrame(state.bottomFrame);
  state.bottomFrame = requestAnimationFrame(/**
   * 下一帧布局完成后清空帧标记，并在仍启用跟随时再次对齐底部。
   */
  () => {
    state.bottomFrame = null;
    applyBottom();
  });
};

/**
 * 列表更新前记录首条仍可见日志及其相对位置。
 *
 * Args:
 *     side (LogSide): 前端或后端日志类别。
 *
 * Returns:
 *     ViewAnchor | null: 可见参考记录，没有可用记录时为空。
 */
const captureViewAnchor = (side: LogSide): ViewAnchor | null => {
  const list = elements[side].list;
  const listTop = list.getBoundingClientRect().top;
  const rows = list.querySelectorAll<HTMLElement>(".log-row[data-record-id]");
  for (const row of rows) {
    const rowRect = row.getBoundingClientRect();
    if (rowRect.bottom > listTop) {
      const recordId = Number(row.dataset.recordId);
      if (Number.isFinite(recordId)) {
        return {
          recordId,
          offset: rowRect.top - listTop,
          scrollTop: list.scrollTop,
        };
      }
    }
  }
  return null;
};

/**
 * 重建列表后按日志编号恢复阅读位置，原记录已淘汰时选邻近记录。
 *
 * Args:
 *     side (LogSide): 前端或后端日志类别。
 *     visibleRecords (readonly LogRecord[]): 本次实际渲染到页面的日志记录。
 *     previousAnchor (ViewAnchor | null): 更新列表前保存的可见参考记录和位置。
 */
const restoreViewAnchor = (
  side: LogSide,
  visibleRecords: readonly LogRecord[],
  previousAnchor: ViewAnchor | null,
): void => {
  if (!previousAnchor) return;
  const list = elements[side].list;
  const targetId = selectRestorationAnchorId(
    visibleRecords.map(/**
     * 提取当前可见日志编号，供寻找更新前阅读位置的邻近记录。
     *
     * Args:
     *     record (LogRecord): 当前需要显示或筛选的日志记录。
     *
     * Returns:
     *     number: 日志稳定编号。
     */
    (record) => record.id),
    previousAnchor.recordId,
  );
  if (targetId === undefined) {
    list.scrollTop = previousAnchor.scrollTop;
    return;
  }
  const target = list.querySelector<HTMLElement>(`[data-record-id="${targetId}"]`);
  if (!target) {
    list.scrollTop = previousAnchor.scrollTop;
    return;
  }
  const currentOffset = target.getBoundingClientRect().top
    - list.getBoundingClientRect().top;
  list.scrollTop = resolveAnchoredScrollTop(
    list.scrollTop,
    currentOffset,
    previousAnchor.offset,
  );
};

/**
 * 以文本节点展示日志时间、级别、来源和正文，不把日志内容当作 HTML。
 *
 * Args:
 *     record (LogRecord): 当前需要显示或筛选的日志记录。
 *
 * Returns:
 *     HTMLDivElement: 带稳定记录编号的日志行元素。
 */
const createLogRow = (record: LogRecord): HTMLDivElement => {
  const row = document.createElement("div");
  row.className = "log-row";
  row.dataset.level = record.level;
  row.dataset.recordId = String(record.id);

  const time = document.createElement("span");
  time.className = "log-time";
  time.textContent = new Date(record.timestamp).toLocaleTimeString("zh-CN", { hour12: false });
  const level = document.createElement("span");
  level.className = "log-level";
  level.textContent = record.level.toUpperCase();
  const source = document.createElement("span");
  source.className = "log-source";
  source.textContent = `[${record.source}/${record.scope}]`;
  const message = document.createElement("span");
  message.className = "log-message";
  message.textContent = record.message;
  row.append(time, level, source, message);
  return row;
};

/**
 * 显示筛选结果总数，超过渲染上限时同时显示实际渲染数量。
 *
 * Args:
 *     side (LogSide): 前端或后端日志类别。
 *     matchingCount (number): 当前过滤条件下匹配的日志总数。
 */
const updateCount = (side: LogSide, matchingCount: number): void => {
  elements[side].count.textContent = matchingCount > MAX_RENDERED_ROWS
    ? `${MAX_RENDERED_ROWS}/${matchingCount} 条`
    : `${matchingCount} 条`;
};

/**
 * 根据当前匹配数量显示或移除没有日志的占位提示。
 *
 * Args:
 *     side (LogSide): 前端或后端日志类别。
 *     matchingCount (number): 当前过滤条件下匹配的日志总数。
 */
const syncEmptyState = (side: LogSide, matchingCount: number): void => {
  const list = elements[side].list;
  const existing = list.querySelector<HTMLElement>(".empty-state");
  if (matchingCount > 0) {
    existing?.remove();
    return;
  }
  if (existing) return;
  const empty = document.createElement("div");
  empty.className = "empty-state ui-empty-state";
  empty.textContent = "当前筛选条件下没有日志";
  list.insertBefore(empty, getBottomAnchor(side));
};

/**
 * 过滤条件变化时重建一类日志，自动跟随时滚到底部，否则恢复用户阅读位置。
 *
 * Args:
 *     side (LogSide): 前端或后端日志类别。
 */
const rebuildSide = (side: LogSide): void => {
  if (!model) return;
  const state = viewStates[side];
  const previousAnchor = state.viewport.isFollowing() ? null : captureViewAnchor(side);
  const records = model.filter(side, getFilter(side));
  const visibleRecords = records.slice(-MAX_RENDERED_ROWS);
  const fragment = document.createDocumentFragment();

  if (visibleRecords.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty-state ui-empty-state";
    empty.textContent = "当前筛选条件下没有日志";
    fragment.append(empty);
  } else {
    for (const record of visibleRecords) fragment.append(createLogRow(record));
  }
  fragment.append(createBottomAnchor());

  elements[side].list.replaceChildren(fragment);
  state.renderedIds = new Set(visibleRecords.map(/**
   * 记录重建后实际渲染的日志编号，供后续增量删除和数量限制。
   *
   * Args:
   *     record (LogRecord): 当前需要显示或筛选的日志记录。
   *
   * Returns:
   *     number: 日志稳定编号。
   */
  (record) => record.id));
  state.matchingCount = records.length;
  updateCount(side, state.matchingCount);

  if (state.viewport.isFollowing()) {
    scrollToBottom(side);
  } else {
    restoreViewAnchor(side, visibleRecords, previousAnchor);
  }
};

/**
 * 重新应用当前筛选条件，分别更新前端与后端日志列表。
 */
const rebuildAll = (): void => {
  rebuildSide("frontend");
  rebuildSide("backend");
};

/**
 * 记录被缓存淘汰时同步移除对应 DOM 行和渲染编号。
 *
 * Args:
 *     side (LogSide): 前端或后端日志类别。
 *     recordId (number): 需要移除的日志记录编号。
 */
const removeRenderedRecord = (side: LogSide, recordId: number): void => {
  const state = viewStates[side];
  if (!state.renderedIds.delete(recordId)) return;
  elements[side].list
    .querySelector<HTMLElement>(`[data-record-id="${recordId}"]`)
    ?.remove();
};

/**
 * 限制日志 DOM 行数量，优先移除最旧已渲染行。
 *
 * Args:
 *     side (LogSide): 前端或后端日志类别。
 */
const trimRenderedRows = (side: LogSide): void => {
  const list = elements[side].list;
  const state = viewStates[side];
  while (state.renderedIds.size > MAX_RENDERED_ROWS) {
    const first = list.querySelector<HTMLElement>(".log-row[data-record-id]");
    if (!first) break;
    const recordId = Number(first.dataset.recordId);
    first.remove();
    if (Number.isFinite(recordId)) state.renderedIds.delete(recordId);
  }
};

/**
 * 收到主进程批次后更新内存记录，只增删受影响的日志行并保持滚动状态。
 *
 * Args:
 *     records (readonly LogRecord[]): 按产生顺序排列的日志记录。
 */
const appendIncomingRecords = (records: readonly LogRecord[]): void => {
  if (!model || records.length === 0) return;
  const delta = model.append(records);
  if (delta.added.length === 0 && delta.removed.length === 0) return;

  const touchedSides = new Set<LogSide>();
  for (const record of delta.removed) {
    if (model.matches(record, getFilter(record.side))) {
      const state = viewStates[record.side];
      state.matchingCount = Math.max(0, state.matchingCount - 1);
      touchedSides.add(record.side);
    }
    removeRenderedRecord(record.side, record.id);
  }
  for (const record of delta.added) {
    const side = record.side;
    if (!model.matches(record, getFilter(side))) continue;
    const state = viewStates[side];
    if (state.renderedIds.has(record.id)) continue;
    state.matchingCount += 1;
    elements[side].list.insertBefore(createLogRow(record), getBottomAnchor(side));
    state.renderedIds.add(record.id);
    touchedSides.add(side);
    trimRenderedRows(side);
  }

  for (const side of touchedSides) {
    const state = viewStates[side];
    syncEmptyState(side, state.matchingCount);
    updateCount(side, state.matchingCount);
    if (viewStates[side].viewport.isFollowing()) scrollToBottom(side);
  }
};

/**
 * 安排结束短暂用户滚动标记，避免后续程序滚动误取消跟随。
 *
 * Args:
 *     side (LogSide): 前端或后端日志类别。
 *     delay (number): 用户滚动意图保持的毫秒数。
 */
const scheduleUserIntentReset = (side: LogSide, delay = 160): void => {
  const state = viewStates[side];
  if (state.userIntentTimer !== null) window.clearTimeout(state.userIntentTimer);
  state.userIntentTimer = window.setTimeout(/**
   * 用户滚动意图保持期到达后清空计时标记，后续程序滚动不再当作用户操作。
   */
  () => {
    state.userIntentTimer = null;
    state.viewport.endUserScroll();
  }, delay);
};

/**
 * 滚轮或滚动按键触发时标记真实用户意图，再安排清除标记。
 *
 * Args:
 *     side (LogSide): 前端或后端日志类别。
 *     delay (number): 用户滚动意图保持的毫秒数。
 */
const beginMomentaryUserScroll = (side: LogSide, delay?: number): void => {
  viewStates[side].viewport.beginUserScroll();
  scheduleUserIntentReset(side, delay);
};

/**
 * 登记滚轮、按键、拖动及尺寸变化回调，区分用户阅读旧日志和程序对齐底部。
 *
 * Args:
 *     side (LogSide): 前端或后端日志类别。
 */
const setupScrollTracking = (side: LogSide): void => {
  const sideElements = elements[side];
  const state = viewStates[side];

  sideElements.list.addEventListener("wheel", /**
   * 用户滚轮操作开始时短暂标记真实滚动意图。
   */
  () => beginMomentaryUserScroll(side), {
    passive: true,
  });
  sideElements.list.addEventListener("keydown", /**
   * 用户按滚动相关键时才标记滚动意图，其他按键不影响自动跟随。
   *
   * Args:
   *     event (KeyboardEvent): 调用方或浏览器触发的当前事件。
   */
  (event) => {
    if (USER_SCROLL_KEYS.has(event.key)) beginMomentaryUserScroll(side);
  });
  sideElements.list.addEventListener("pointerdown", /**
   * 用户开始拖动滚动条时标记主动滚动。
   */
  () => {
    state.viewport.beginUserScroll();
  });
  window.addEventListener("pointerup", /**
   * 用户结束鼠标拖动时清除滚动标记。
   */
  () => state.viewport.endUserScroll());
  window.addEventListener("pointercancel", /**
   * 指针操作被取消时清除滚动标记。
   */
  () => state.viewport.endUserScroll());
  sideElements.list.addEventListener("touchstart", /**
   * 用户开始触摸滚动时标记主动滚动。
   */
  () => {
    state.viewport.beginUserScroll();
  }, { passive: true });
  sideElements.list.addEventListener("touchend", /**
   * 触摸结束后短暂保留滚动意图，覆盖惯性滚动阶段。
   */
  () => {
    scheduleUserIntentReset(side, 300);
  }, { passive: true });
  sideElements.list.addEventListener("scroll", /**
   * 滚动事件到达后计算离底部距离，只在用户主动离开底部时同步取消跟随选项。
   */
  () => {
    const distance = sideElements.list.scrollHeight
      - sideElements.list.scrollTop
      - sideElements.list.clientHeight;
    if (state.viewport.handleScroll(distance)) {
      sideElements.autoscroll.checked = false;
    }
  });

  if (typeof ResizeObserver !== "undefined") {
    const observer = new ResizeObserver(/**
     * 列表尺寸改变后仅在自动跟随启用时重新滚到底部。
     */
    () => {
      if (state.viewport.isFollowing()) scrollToBottom(side);
    });
    observer.observe(sideElements.list);
    resizeObservers.push(observer);
  }
};

/**
 * 绑定搜索、筛选、暂停、清空和打开日志目录的页面操作。
 */
const setupControls = (): void => {
  search.addEventListener("input", rebuildAll);
  pauseButton.addEventListener("click", /**
   * 用户点击暂停或继续时切换模型暂存状态，更新按钮并重建可见日志。
   */
  () => {
    if (!model) return;
    const paused = !model.isPaused();
    model.setPaused(paused);
    pauseButton.textContent = paused ? "继续刷新" : "暂停刷新";
    pauseButton.setAttribute("aria-pressed", String(paused));
    status.textContent = paused ? "界面已暂停，日志仍在持续写入文件" : "实时日志流已连接";
    if (!paused) rebuildAll();
  });
  getElement("#open-log-directory-btn").addEventListener("click", /**
   * 用户点击日志目录入口时请求主进程打开文件夹，失败交给状态栏显示。
   */
  () => {
    void window.desktopPetApi.openLogDirectory().catch(/**
     * 打开日志目录失败后在状态栏显示可读错误。
     *
     * Args:
     *     error (unknown): 本次捕获的异常。
     */
    (error: unknown) => {
      status.textContent = error instanceof Error ? error.message : "无法打开日志目录";
    });
  });
  getElement("#log-min-btn").addEventListener("click", /**
   * 用户点击标题栏最小化按钮时请求主进程最小化本窗口。
   */
  () => {
    window.desktopPetApi.minimizeCurrentWindow();
  });
  getElement("#log-close-btn").addEventListener("click", /**
   * 用户点击标题栏关闭按钮时请求主进程关闭本窗口。
   */
  () => {
    window.desktopPetApi.closeCurrentWindow();
  });

  for (const side of ["frontend", "backend"] as const) {
    const sideElements = elements[side];
    const state = viewStates[side];
    sideElements.level.addEventListener("change", /**
     * 日志级别选项变化后按新条件重建当前类别列表。
     */
    () => rebuildSide(side));
    sideElements.source.addEventListener("change", /**
     * 日志来源选项变化后按新条件重建当前类别列表。
     */
    () => rebuildSide(side));
    sideElements.autoscroll.addEventListener("change", /**
     * 用户切换自动跟随时更新滚动状态，启用后立即滚到最新日志。
     */
    () => {
      state.viewport.setFollowing(sideElements.autoscroll.checked);
      if (state.viewport.isFollowing()) scrollToBottom(side);
    });
    setupScrollTracking(side);
    sideElements.clear.addEventListener("click", /**
     * 用户点击清空时先等待主进程清空，再更新本页缓存和显示；失败保留本地记录。
     */
    () => {
      void window.desktopPetApi.clearLogBuffer(side).then(/**
       * 主进程确认清空成功后清理本页同类记录并重建列表。
       */
      () => {
        model?.clear(side);
        rebuildSide(side);
      }).catch(/**
       * 清空请求失败后在状态栏显示错误，不假定主进程已经清空。
       *
       * Args:
       *     error (unknown): 本次捕获的异常。
       */
      (error: unknown) => {
        status.textContent = error instanceof Error ? error.message : "清空日志视图失败";
      });
    });
  }
};

/**
 * 加载主进程快照后初始化日志模型和控件，并订阅后续日志批次。
 *
 * Returns:
 *     Promise<void>: 日志窗口初始化完成后结束。
 */
const start = async (): Promise<void> => {
  // 1. 日志快照读取前先安装窗口拖动，连接失败时仍能移动和缩放日志窗口。
  const releaseWindowGestures = setupWindowGestures(getElement(".log-window"));
  const earlyRecords: LogRecord[] = [];
  let acceptingEarly = true;
  const unsubscribe = window.desktopPetApi.onLogBatch(/**
   * 实时日志在初始快照读取期间先暂存，快照初始化完成后才进入正常增量显示。
   *
   * Args:
   *     batch (LogBatch): 包含已脱敏日志记录的实时批次。
   */
  (batch) => {
    if (acceptingEarly) {
      earlyRecords.push(...batch.records);
      return;
    }
    appendIncomingRecords(batch.records);
  });
  window.addEventListener("beforeunload", /**
   * 日志窗口卸载时停止窗口拖动、取消日志订阅，并断开所有尺寸观察器。
   */
  () => {
    releaseWindowGestures();
    unsubscribe();
    for (const observer of resizeObservers) observer.disconnect();
  }, { once: true });

  const snapshot = await window.desktopPetApi.getLogSnapshot();
  model = new LogViewerModel(snapshot);
  model.append(earlyRecords);
  acceptingEarly = false;
  setupControls();
  rebuildAll();
  status.textContent = "实时日志流已连接";
};

setupApplicationShutdownUi();
void start().catch(/**
 * 日志窗口初始化失败后在状态栏显示错误原因。
 *
 * Args:
 *     error (unknown): 本次捕获的异常。
 */
(error: unknown) => {
  status.textContent = error instanceof Error ? error.message : "日志控制台初始化失败";
});

initializeIcons();
