import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtempSync, readFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";

test("窗口位置和设置页面写入互相保留且不保存最小化状态", /** 在独立临时目录验证真实配置读写，不访问生产前端数据。 */
async () => {
  const directory = mkdtempSync(path.join(os.tmpdir(), "ayaya-ui-store-test-"));
  process.env.AYAYA_UI_DATA_DIR = directory;
  const { readFrontendSettings, saveFrontendSettings } = await import("../electron/frontend-settings-store.js");
  const bounds = { x: -600, y: 80, width: 560, height: 720 };
  saveFrontendSettings({ chat_window_bounds: bounds });
  saveFrontendSettings({ hide_on_screenshot: false });
  saveFrontendSettings({ chat_window_pinned: true });
  assert.deepEqual(readFrontendSettings(), { hide_on_screenshot: false, chat_window_bounds: bounds, chat_window_pinned: true });
  const disk = JSON.parse(readFileSync(path.join(directory, "frontend_settings.json"), "utf8"));
  assert.equal("minimized" in disk, false);
  assert.deepEqual(disk.chat_window_bounds, bounds);
});
