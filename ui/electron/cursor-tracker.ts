/**
 * 光标位置追踪
 */

import { screen } from "electron";

import { getMainWindow } from "./window-manager.js";
import type { CursorSyncPayload } from "./types.js";

/**
 * 光标同步定时器
 */
let cursorSyncTimer: NodeJS.Timeout | null = null;

/**
 * 开始光标追踪
 */
export const startCursorTracking = (): void => {
  if (cursorSyncTimer) {
    return;
  }

  cursorSyncTimer = setInterval(() => {
    const mainWindow = getMainWindow();
    if (!mainWindow || mainWindow.isDestroyed()) {
      return;
    }

    const cursor = screen.getCursorScreenPoint();
    const bounds = mainWindow.getBounds();
    const display = screen.getDisplayNearestPoint(cursor);
    const workArea = display.workArea;
    const localX = cursor.x - bounds.x;
    const localY = cursor.y - bounds.y;
    const insideWindow = localX >= 0 && localX <= bounds.width && localY >= 0 && localY <= bounds.height;

    const payload: CursorSyncPayload = {
      localX,
      localY,
      screenX: cursor.x,
      screenY: cursor.y,
      windowX: bounds.x,
      windowY: bounds.y,
      windowWidth: bounds.width,
      windowHeight: bounds.height,
      displayX: workArea.x,
      displayY: workArea.y,
      displayWidth: workArea.width,
      displayHeight: workArea.height,
      insideWindow,
    };

    mainWindow.webContents.send("desktop-pet:cursor", payload);
  }, 16);
};

/**
 * 停止光标追踪
 */
export const stopCursorTracking = (): void => {
  if (cursorSyncTimer) {
    clearInterval(cursorSyncTimer);
    cursorSyncTimer = null;
  }
};

/**
 * 获取光标追踪定时器
 */
export const getCursorSyncTimer = (): NodeJS.Timeout | null => {
  return cursorSyncTimer;
};
