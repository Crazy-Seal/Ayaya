import assert from "node:assert/strict";
import { test } from "node:test";

import { pasteScreenActionText } from "../electron/screen-action-input.js";

const keys = {
  leftControl: "LeftControl",
  v: "V",
  enter: "Enter",
};

/**
 * 创建可记录读取及写入的纯文本剪贴板替身。
 *
 * Args:
 *     initialText (string): 动作前测试剪贴板已经包含的文本。
 */
const createClipboard = (initialText = "old text") => {
  const writes: string[] = [];
  let text = initialText;

  return {
    writes,
    api: {
      /**
       * 读取测试剪贴板或页面当前保存的文本。
       *
       * 此定义用于：创建可记录读取及写入的纯文本剪贴板替身。
       */
      readText: () => text,
      /**
       * 记录本次写入，并更新测试剪贴板的文本。
       *
       * 此定义用于：创建可记录读取及写入的纯文本剪贴板替身。
       *
       * Args:
       *     nextText (string): 本次模拟写入剪贴板的新文本。
       */
      writeText: (nextText: string) => {
        writes.push(nextText);
        text = nextText;
      },
    },
  };
};

/**
 * 创建支持文本、HTML、RTF 和图片恢复的剪贴板替身。
 */
const createRichClipboard = () => {
  const image = { id: "old image" };
  const writes: unknown[] = [];
  const events: Array<[string, string]> = [];
  let text = "old text";

  return {
    image,
    writes,
    events,
    api: {
      /**
       * 读取测试剪贴板或页面当前保存的文本。
       *
       * 此定义用于：创建支持文本、HTML、RTF 和图片恢复的剪贴板替身。
       */
      readText: () => text,
      /**
       * 记录本次写入，并更新测试剪贴板的文本。
       *
       * 此定义用于：创建支持文本、HTML、RTF 和图片恢复的剪贴板替身。
       *
       * Args:
       *     nextText (string): 本次模拟写入剪贴板的新文本。
       */
      writeText: (nextText: string) => {
        events.push(["writeText", nextText]);
        text = nextText;
      },
      /**
       * 返回本用例指定的剪贴板格式集合。
       *
       * 此定义用于：创建支持文本、HTML、RTF 和图片恢复的剪贴板替身。
       */
      availableFormats: () => ["text/plain", "text/html", "text/rtf", "image/png"],
      /**
       * 返回测试剪贴板的 HTML 内容。
       *
       * 此定义用于：创建支持文本、HTML、RTF 和图片恢复的剪贴板替身。
       */
      readHTML: () => "<b>old html</b>",
      /**
       * 返回测试剪贴板的 RTF 内容。
       *
       * 此定义用于：创建支持文本、HTML、RTF 和图片恢复的剪贴板替身。
       */
      readRTF: () => "{\\rtf1 old}",
      /**
       * 返回测试剪贴板的图片。
       *
       * 此定义用于：创建支持文本、HTML、RTF 和图片恢复的剪贴板替身。
       */
      readImage: () => image,
      /**
       * 记录一次内容写入，供验证富剪贴板恢复。
       *
       * 此定义用于：创建支持文本、HTML、RTF 和图片恢复的剪贴板替身。
       *
       * Args:
       *     data (unknown): 调用方提供的页面或接口数据。
       */
      write: (data: unknown) => {
        events.push(["write", JSON.stringify(data)]);
        writes.push(data);
      },
    },
  };
};

/**
 * 创建包含文件拖放格式的剪贴板，用于验证粘贴被拒绝。
 */
const createFileClipboard = () => {
  const events: Array<[string, string]> = [];

  return {
    events,
    api: {
      /**
       * 读取测试剪贴板或页面当前保存的文本。
       *
       * 此定义用于：创建包含文件拖放格式的剪贴板，用于验证粘贴被拒绝。
       */
      readText: () => "",
      /**
       * 记录本次写入，并更新测试剪贴板的文本。
       *
       * 此定义用于：创建包含文件拖放格式的剪贴板，用于验证粘贴被拒绝。
       *
       * Args:
       *     nextText (string): 本次模拟写入剪贴板的新文本。
       */
      writeText: (nextText: string) => {
        events.push(["writeText", nextText]);
      },
      /**
       * 返回本用例指定的剪贴板格式集合。
       *
       * 此定义用于：创建包含文件拖放格式的剪贴板，用于验证粘贴被拒绝。
       */
      availableFormats: () => ["FileNameW"],
      /**
       * 返回测试剪贴板的 HTML 内容。
       *
       * 此定义用于：创建包含文件拖放格式的剪贴板，用于验证粘贴被拒绝。
       */
      readHTML: () => "",
      /**
       * 返回测试剪贴板的 RTF 内容。
       *
       * 此定义用于：创建包含文件拖放格式的剪贴板，用于验证粘贴被拒绝。
       */
      readRTF: () => "",
      /**
       * 返回测试剪贴板的图片。
       *
       * 此定义用于：创建包含文件拖放格式的剪贴板，用于验证粘贴被拒绝。
       */
      readImage: () => ({ id: "unused image" }),
      /**
       * 记录一次内容写入，供验证富剪贴板恢复。
       *
       * 此定义用于：创建包含文件拖放格式的剪贴板，用于验证粘贴被拒绝。
       *
       * Args:
       *     data (unknown): 调用方提供的页面或接口数据。
       */
      write: (data: unknown) => {
        events.push(["write", JSON.stringify(data)]);
      },
    },
  };
};

/**
 * 创建记录按键顺序并可制造异常的键盘替身。
 *
 * Args:
 *     failOnPress (unknown): 本用例要主动制造按键按下异常的位置。
 */
const createKeyboard = (failOnPress?: unknown) => {
  const events: Array<[string, unknown]> = [];

  return {
    events,
    api: {
      /**
       * 记录测试键盘逐字输入调用，供断言文本粘贴不会走此方法。
       *
       * 此定义用于：创建记录按键顺序并可制造异常的键盘替身。
       *
       * Args:
       *     _text (string): 需要显示、输入或转换的文本。
       */
      type: async (_text: string) => {
        events.push(["type", _text]);
      },
      /**
       * 记录按键按下操作，按用例配置可主动抛错。
       *
       * 此定义用于：创建记录按键顺序并可制造异常的键盘替身。
       *
       * Args:
       *     key (unknown): 当前配置字段或索引名称。
       */
      pressKey: async (key: unknown) => {
        events.push(["press", key]);
        if (key === failOnPress) {
          throw new Error("press failed");
        }
      },
      /**
       * 记录按键释放操作，供检查失败路径的清理。
       *
       * 此定义用于：创建记录按键顺序并可制造异常的键盘替身。
       *
       * Args:
       *     key (unknown): 当前配置字段或索引名称。
       */
      releaseKey: async (key: unknown) => {
        events.push(["release", key]);
      },
    },
  };
};

test("文本输入写入剪贴板并通过 Ctrl+V 粘贴，不调用 keyboard.type", /**
 * 验证文本输入写入剪贴板并通过 Ctrl+V 粘贴，不调用 keyboard.type。
 */
async () => {
  const clipboard = createClipboard();
  const keyboard = createKeyboard();

  await pasteScreenActionText({
    text: "庆祝你满血复活",
    pressEnter: false,
    clipboard: clipboard.api,
    keyboard: keyboard.api,
    keys,
  });

  assert.deepEqual(clipboard.writes, ["庆祝你满血复活", "old text"]);
  assert.deepEqual(keyboard.events, [
    ["press", "LeftControl"],
    ["press", "V"],
    ["release", "V"],
    ["release", "LeftControl"],
  ]);
});

test("press_enter=true 时粘贴后按 Enter", /**
 * 验证press_enter=true 时粘贴后按 Enter。
 */
async () => {
  const clipboard = createClipboard();
  const keyboard = createKeyboard();

  await pasteScreenActionText({
    text: "hello",
    pressEnter: true,
    clipboard: clipboard.api,
    keyboard: keyboard.api,
    keys,
  });

  assert.deepEqual(keyboard.events.slice(-2), [
    ["press", "Enter"],
    ["release", "Enter"],
  ]);
});

test("粘贴失败时仍释放已按下的 Ctrl 并恢复剪贴板文本", /**
 * 验证粘贴失败时仍释放已按下的 Ctrl 并恢复剪贴板文本。
 */
async () => {
  const clipboard = createClipboard("before");
  const keyboard = createKeyboard("V");

  await assert.rejects(
    pasteScreenActionText({
      text: "庆祝你满血复活",
      pressEnter: true,
      clipboard: clipboard.api,
      keyboard: keyboard.api,
      keys,
    }),
    /press failed/,
  );

  assert.deepEqual(clipboard.writes, ["庆祝你满血复活", "before"]);
  assert.deepEqual(keyboard.events, [
    ["press", "LeftControl"],
    ["press", "V"],
    ["release", "LeftControl"],
  ]);
});

test("恢复剪贴板时使用 clipboard.write(data) 一次性保留 HTML、RTF 和图片格式", /**
 * 验证恢复剪贴板时使用 clipboard.write(data) 一次性保留 HTML、RTF 和图片格式。
 */
async () => {
  const clipboard = createRichClipboard();
  const keyboard = createKeyboard();

  await pasteScreenActionText({
    text: "hello",
    pressEnter: false,
    clipboard: clipboard.api,
    keyboard: keyboard.api,
    keys,
  });

  assert.equal(clipboard.writes.length, 1);
  assert.deepEqual(clipboard.writes[0], {
    text: "old text",
    html: "<b>old html</b>",
    rtf: "{\\rtf1 old}",
    image: clipboard.image,
  });
  assert.deepEqual(clipboard.events, [
    ["writeText", "hello"],
    ["write", JSON.stringify({
      text: "old text",
      html: "<b>old html</b>",
      rtf: "{\\rtf1 old}",
      image: clipboard.image,
    })],
  ]);
});

test("检测到 Windows 文件剪贴板格式时拒绝粘贴以避免覆盖无法可靠恢复的数据", /**
 * 验证检测到 Windows 文件剪贴板格式时拒绝粘贴以避免覆盖无法可靠恢复的数据。
 */
async () => {
  const clipboard = createFileClipboard();
  const keyboard = createKeyboard();

  await assert.rejects(
    pasteScreenActionText({
      text: "hello",
      pressEnter: false,
      clipboard: clipboard.api,
      keyboard: keyboard.api,
      keys,
    }),
    /当前用户剪贴板中包含文件，无法在粘贴后可靠恢复。为避免覆盖剪贴板，已取消这次文本输入。/,
  );

  assert.deepEqual(clipboard.events, []);
  assert.deepEqual(keyboard.events, []);
});
