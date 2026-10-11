/** 用真实 Windows 鼠标拖边与移动窗口，捕获原生画面而非仅设置窗口尺寸。 */
import { BrowserWindow, screen } from "electron";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { setTimeout as pause } from "node:timers/promises";
const execute = promisify(execFile);

/**
 * 在纯色测试桌面上操作当前聊天窗口，检测对边白色闪烁和屏幕左侧残影。
 * Args:
 *     options ({ chat: BrowserWindow; pet: BrowserWindow; output: string }): 已隔离的真实应用窗口和输出位置。
 */
export const verifyNativeChatDrag = async ({ chat, pet, output }) => {
  assert.equal(process.platform, "win32", "原生拖动验证只适用于 Windows");
  output = path.join(output, process.argv.includes("--dev") ? "development" : "production");
  fs.mkdirSync(output, { recursive: true });
  const display = screen.getPrimaryDisplay();
  const area = display.workArea;
  // 1. 开发者工具保持开启以匹配 npm run dev；等其首次加载后再把测试背景放到前台。
  for (let attempt = 0; attempt < 30 && pet.webContents.devToolsWebContents?.isLoadingMainFrame(); attempt++) await pause(100);
  // 1. 用本应用纯色背景遮住工作区，截图只包含测试窗口，不记录其他应用内容。
  const background = new BrowserWindow({ ...area, frame: false, resizable: false, backgroundColor: "#15324c", show: true,
    webPreferences: { contextIsolation: true, nodeIntegration: false } });
  await background.loadURL("data:text/html,<body style='margin:0;background:%2315324c'></body>");
  background.setBounds(area);
  background.moveTop(); chat.show(); chat.moveTop(); chat.focus();
  const { startCursorTracking, stopCursorTracking } = await import("../dist-electron/electron/cursor-tracker.js");
  startCursorTracking();
  const nativeHandle = chat.getNativeWindowHandle().readBigUInt64LE().toString();
  const nativePetHandle = pet.getNativeWindowHandle().readBigUInt64LE().toString();
  const worker = fileURLToPath(new URL("./windows-native-drag.ps1", import.meta.url));
  const physicalArea = screen.dipToScreenRect(null, area);
  const results = [];
  try {
    const modes = process.argv.includes("--left-only") ? Array.from({ length: 5 }, /** 重复同一路径以提高绘制闪烁的复现率。 */ () => "left") : ["left", "top", "right", "bottom", "overlap"];
    for (const mode of modes) {
      // 2. 各方向从同一合法尺寸开始，避免上一轮缩放影响检测位置。
      chat.setContentBounds({ x: area.x + 240, y: area.y + 70, width: 560, height: 610 });
      background.moveTop(); chat.moveTop(); chat.focus();
      await pause(250);
      const { stdout, stderr } = await execute("powershell.exe", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", worker,
        "-NativeHandle", nativeHandle, "-NativePetHandle", nativePetHandle, "-Mode", mode, "-Scale", String(display.scaleFactor),
        "-AreaX", String(physicalArea.x), "-AreaY", String(physicalArea.y),
        "-AreaWidth", String(physicalArea.width), "-AreaHeight", String(physicalArea.height),
        "-OutputDirectory", output], { windowsHide: true, timeout: 30000, maxBuffer: 2 * 1024 * 1024 });
      if (stderr.trim()) console.log(stderr.trim());
      const result = JSON.parse(stdout.trim()); results.push(result);
      console.log(JSON.stringify(result));
    }
    fs.writeFileSync(path.join(output, process.argv.includes("--left-only") ? "results-left-repeat.json" : "results.json"), JSON.stringify(results, null, 2));
    // 3. 白色边带必须与静止基线相比；左侧区域只允许显示纯色背景。
    for (const result of results) {
      assert.ok(result.draggedDistance > 50, `${result.mode} 没有实际拖动原生窗口`);
      assert.ok(result.whitePeak <= result.whiteBaseline + 80, `${result.mode} 拖动时出现额外白边：${result.whitePeak}`);
      assert.ok(result.ghostPeak < 100, `${result.mode} 拖动时左侧出现非背景残影：${result.ghostPeak}`);
      assert.ok(result.oppositeDrift <= 3, `${result.mode} 拖动时对边位置震荡：${result.oppositeDrift}`);
      assert.equal(result.pointerInterceptions, 0, `${result.mode} 拖动期间角色窗口重新捕获鼠标`);
    }
  } finally {
    stopCursorTracking(); background.destroy();
    // 4. 模拟桌面与鼠标 worker 都结束后，由外层验证脚本清理应用窗口。
    if (!pet.isDestroyed()) pet.setIgnoreMouseEvents(true, { forward: true });
  }
};
