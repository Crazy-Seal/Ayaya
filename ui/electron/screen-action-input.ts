export type TextClipboard = {
  readText: () => string;
  writeText: (text: string) => void;
  availableFormats?: () => string[];
  readHTML?: () => string;
  readRTF?: () => string;
  readImage?: () => unknown;
  write?: (data: ClipboardWriteData) => void;
};

export type PasteKeyboard<KeyType> = {
  pressKey: (...keys: KeyType[]) => Promise<unknown>;
  releaseKey: (...keys: KeyType[]) => Promise<unknown>;
};

export type PasteKeys<KeyType> = {
  leftControl: KeyType;
  v: KeyType;
  enter: KeyType;
};

export type PasteScreenActionTextOptions<KeyType> = {
  text: string;
  pressEnter: boolean;
  clipboard: TextClipboard;
  keyboard: PasteKeyboard<KeyType>;
  keys: PasteKeys<KeyType>;
};

type ClipboardWriteData = {
  text?: string;
  html?: string;
  rtf?: string;
  image?: unknown;
};

type ClipboardSnapshot =
  | {
      kind: "data";
      data: ClipboardWriteData;
    }
  | {
      kind: "text";
      text: string;
    };

/**
 * 检查剪贴板接口是否支持读取格式并一次恢复多种内容。
 *
 * Args:
 *     clipboard (TextClipboard): 桌面剪贴板访问接口。
 *
 * Returns:
 *     clipboard is TextClipboard & Required<Pick<TextClipboard, "availableFormats" | "write">>: 可保存和恢复富文本剪贴板时为 true。
 */
const canSnapshotData = (
  clipboard: TextClipboard,
): clipboard is TextClipboard & Required<Pick<TextClipboard, "availableFormats" | "write">> =>
  typeof clipboard.availableFormats === "function" &&
  typeof clipboard.write === "function";

/**
 * 统一剪贴板格式大小写，供类型比较。
 *
 * Args:
 *     format (string): 剪贴板格式名称。
 *
 * Returns:
 *     string: 小写格式名称。
 */
const normalizeFormat = (format: string): string => format.toLowerCase();

/**
 * 识别文件拖放相关剪贴板格式，避免文本粘贴破坏无法恢复的文件列表。
 *
 * Args:
 *     format (string): 剪贴板格式名称。
 *
 * Returns:
 *     boolean: 格式包含文件名或拖放标记时为 true。
 */
const isFileClipboardFormat = (format: string): boolean => {
  const normalized = normalizeFormat(format);
  return normalized.includes("filename") || normalized.includes("hdrop");
};

/**
 * 检查剪贴板格式是否为能够保存恢复的文本、HTML、RTF 或图片。
 *
 * Args:
 *     format (string): 剪贴板格式名称。
 *
 * Returns:
 *     boolean: 当前恢复流程支持的格式为 true。
 */
const isSupportedClipboardFormat = (format: string): boolean => {
  const normalized = normalizeFormat(format);
  return (
    normalized === "text/plain" ||
    normalized === "text/html" ||
    normalized === "text/rtf" ||
    normalized.startsWith("image/")
  );
};

/**
 * 检查格式列表中是否有符合指定判断规则的内容。
 *
 * Args:
 *     formats (string[]): 剪贴板当前包含的格式名称。
 *     predicate ((format: string) => boolean): 判断当前剪贴板格式是否符合目标类型的函数。
 *
 * Returns:
 *     boolean: 至少一个格式匹配时为 true。
 */
const hasFormat = (formats: string[], predicate: (format: string) => boolean): boolean =>
  formats.some(/**
   * 逐项统一剪贴板格式大小写，再交给目标格式判断函数。
   *
   * Args:
   *     format (string): 剪贴板格式名称。
   *
   * Returns:
   *     boolean: 当前格式满足判断条件时为 true。
   */
  (format) => predicate(normalizeFormat(format)));

/**
 * 执行粘贴前读取原剪贴板；存在无法可靠恢复的格式时立即拒绝输入。
 *
 * Args:
 *     clipboard (TextClipboard): 桌面剪贴板访问接口。
 *
 * Returns:
 *     ClipboardSnapshot: 可以用于动作结束后恢复的剪贴板内容。
 */
const createClipboardSnapshot = (clipboard: TextClipboard): ClipboardSnapshot => {
  if (!canSnapshotData(clipboard)) {
    return {
      kind: "text",
      text: clipboard.readText(),
    };
  }

  const formats = clipboard.availableFormats();
  const unsupportedFormat = formats.find(/**
   * 查找无法完整保存恢复的剪贴板格式，存在时在覆盖内容前拒绝粘贴。
   *
   * Args:
   *     format (string): 剪贴板格式名称。
   *
   * Returns:
   *     boolean: 当前格式不受恢复流程支持时为 true。
   */
  (format) => !isSupportedClipboardFormat(format));
  if (unsupportedFormat) {
    const message = isFileClipboardFormat(unsupportedFormat)
      ? "当前用户剪贴板中包含文件，无法在粘贴后可靠恢复。为避免覆盖剪贴板，已取消这次文本输入。"
      : `当前用户剪贴板中包含当前无法可靠恢复的格式（${unsupportedFormat}），无法在粘贴后可靠恢复。为避免覆盖剪贴板，已取消这次文本输入。`;
    throw new Error(message);
  }

  const data: ClipboardWriteData = {};
  if (hasFormat(formats, /**
   * 识别剪贴板中的纯文本，决定是否保存原文本。
   *
   * Args:
   *     format (string): 剪贴板格式名称。
   *
   * Returns:
   *     boolean: 当前格式为 text/plain 时为 true。
   */
  (format) => format === "text/plain")) {
    data.text = clipboard.readText();
  }
  if (hasFormat(formats, /**
   * 识别剪贴板中的 HTML，决定是否保存原富文本。
   *
   * Args:
   *     format (string): 剪贴板格式名称。
   *
   * Returns:
   *     boolean: 当前格式为 text/html 时为 true。
   */
  (format) => format === "text/html") && typeof clipboard.readHTML === "function") {
    data.html = clipboard.readHTML();
  }
  if (hasFormat(formats, /**
   * 识别剪贴板中的 RTF，决定是否保存原富文本。
   *
   * Args:
   *     format (string): 剪贴板格式名称。
   *
   * Returns:
   *     boolean: 当前格式为 text/rtf 时为 true。
   */
  (format) => format === "text/rtf") && typeof clipboard.readRTF === "function") {
    data.rtf = clipboard.readRTF();
  }
  if (hasFormat(formats, /**
   * 识别剪贴板图片格式，决定是否保存原图片。
   *
   * Args:
   *     format (string): 剪贴板格式名称。
   *
   * Returns:
   *     boolean: 当前格式属于 image 类别时为 true。
   */
  (format) => format.startsWith("image/")) && typeof clipboard.readImage === "function") {
    data.image = clipboard.readImage();
  }

  return {
    kind: "data",
    data,
  };
};

/**
 * 文字输入动作结束后恢复原剪贴板，按保存方式恢复纯文本或富内容。
 *
 * Args:
 *     clipboard (TextClipboard): 桌面剪贴板访问接口。
 *     snapshot (ClipboardSnapshot): 初始化时从主进程取得的前端和后端日志集合。
 */
const restoreClipboardSnapshot = (
  clipboard: TextClipboard,
  snapshot: ClipboardSnapshot,
): void => {
  if (snapshot.kind === "text" || !canSnapshotData(clipboard)) {
    if (snapshot.kind !== "text") {
      return;
    }
    clipboard.writeText(snapshot.text);
    return;
  }

  clipboard.write(snapshot.data);
};

/**
 * 清理阶段仅释放本次已经按下的键，避免异常路径留下按键状态。
 *
 * Args:
 *     keyboard (PasteKeyboard<KeyType>): 执行粘贴所需的键盘接口。
 *     key (KeyType): 当前配置字段或索引名称。
 *     pressed (boolean): 该按键当前是否仍处于按下状态。
 *
 * Returns:
 *     Promise<unknown>: 释放完成后结束，未按下时直接结束。
 */
const releaseIfPressed = async <KeyType>(
  keyboard: PasteKeyboard<KeyType>,
  key: KeyType,
  pressed: boolean,
): Promise<unknown> => {
  if (pressed) {
    await keyboard.releaseKey(key);
  }
  return undefined;
};

/**
 * 按收集顺序重新抛出清理错误，让调用方知道文本输入未可靠完成。
 *
 * Args:
 *     errors (unknown[]): 本次动作与清理阶段捕获的异常列表。
 */
const throwFirstError = (errors: unknown[]): void => {
  if (errors.length > 0) {
    throw errors[0];
  }
};

/**
 * 先保存剪贴板，再粘贴文本；成功或异常都释放按键并恢复原内容，按配置追加回车。
 *
 * Args:
 *     text (string): 需要显示、输入或转换的文本。
 *     pressEnter (boolean): 粘贴成功后是否追加回车动作。
 *     clipboard (TextClipboard): 桌面剪贴板访问接口。
 *     keyboard (PasteKeyboard<KeyType>): 执行粘贴所需的键盘接口。
 *     keys (PasteKeys<KeyType>): 执行粘贴使用的修饰键与字母键。
 *
 * Returns:
 *     Promise<void>: 文本输入和清理结束后完成。
 */
export const pasteScreenActionText = async <KeyType>({
  text,
  pressEnter,
  clipboard,
  keyboard,
  keys,
}: PasteScreenActionTextOptions<KeyType>): Promise<void> => {
  if (!text) {
    return;
  }

  // 1. 先确认原剪贴板能够可靠恢复并保存内容，不支持的文件等格式在覆盖前就拒绝输入。
  const clipboardSnapshot = createClipboardSnapshot(clipboard);
  let controlPressed = false;
  let vPressed = false;

  try {
    // 2. 临时写入文本并按下粘贴组合键，分别记下成功按下的键，供异常路径清理。
    clipboard.writeText(text);
    await keyboard.pressKey(keys.leftControl);
    controlPressed = true;
    await keyboard.pressKey(keys.v);
    vPressed = true;
  } finally {
    // 3. 成功或失败都尝试释放两个按键，随后恢复原剪贴板；释放失败在恢复后再报告。
    const releaseErrors = await Promise.allSettled([
      releaseIfPressed(keyboard, keys.v, vPressed),
      releaseIfPressed(keyboard, keys.leftControl, controlPressed),
    ]);
    restoreClipboardSnapshot(clipboard, clipboardSnapshot);
    throwFirstError(
      releaseErrors
        .filter(/**
         * 按键清理结束后只保留失败项，成功释放不产生待抛异常。
         *
         * Args:
         *     result (PromiseSettledResult<unknown>): 当前异步操作或接口调用返回的结果。
         *
         * Returns:
         *     result is PromiseRejectedResult: 清理 Promise 被拒绝时为 true。
         */
        (result): result is PromiseRejectedResult => result.status === "rejected")
        .map(/**
         * 从失败清理项取得原异常，交给调用方按顺序报告。
         *
         * Args:
         *     result (PromiseRejectedResult): 当前异步操作或接口调用返回的结果。
         *
         * Returns:
         *     any: 该清理步骤的拒绝原因。
         */
        (result) => result.reason),
    );
  }

  // 4. 粘贴和清理没有抛错时才追加回车，避免失败路径继续向未知窗口提交内容。
  if (pressEnter) {
    await keyboard.pressKey(keys.enter);
    await keyboard.releaseKey(keys.enter);
  }
};
