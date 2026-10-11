/** 保存聊天窗口位置与置顶状态，迁移完成后截图开关由后端 YAML 保存。 */
import fs from "node:fs";
import path from "node:path";
import { FRONTEND_SETTINGS_PATH } from "./config.js";
import type { FrontendSettings } from "../shared-types.js";

/**
 * 读取本地窗口配置，缺失或损坏时返回空字段，由窗口创建逻辑使用默认位置。
 * Returns:
 *     FrontendSettings: 当前前端设置。
 */
export const readFrontendSettings = (): FrontendSettings => {
  try {
    return JSON.parse(fs.readFileSync(FRONTEND_SETTINGS_PATH, "utf8")) as FrontendSettings;
  } catch { return {}; }
};

/**
 * 对当前完整 JSON 原子保存，窗口更新与迁移字段清理共用同一写入步骤。
 * Args:
 *     settings (FrontendSettings): 已合并或移除旧字段的完整窗口配置。
 */
const writeFrontendSettings = (settings: FrontendSettings): void => {
  fs.mkdirSync(path.dirname(FRONTEND_SETTINGS_PATH), { recursive: true });
  const temporary = `${FRONTEND_SETTINGS_PATH}.tmp`;
  fs.writeFileSync(temporary, JSON.stringify(settings, null, 2), "utf8");
  fs.renameSync(temporary, FRONTEND_SETTINGS_PATH);
};

/**
 * 合并并原子保存前端配置，主进程串行调用避免窗口与页面互相覆盖。
 * Args:
 *     changes (Partial<FrontendSettings>): 本次需要更新的前端字段。
 * Returns:
 *     FrontendSettings: 已成功写入的完整配置。
 */
export const saveFrontendSettings = (changes: Partial<FrontendSettings>): FrontendSettings => {
  // 1. 每次重新读取完整配置，保留其他入口刚刚保存的字段。
  const settings = { ...readFrontendSettings(), ...changes };
  // 2. 同目录临时文件写完后替换，异常时保留原文件。
  writeFrontendSettings(settings);
  return settings;
};

/** 后端已经确认迁移成功后，重新读取当前 JSON，只删除截图旧字段。 */
export const removeLegacyScreenshotSetting = (): void => {
  const current = readFrontendSettings() as FrontendSettings & { hide_on_screenshot?: boolean };
  if (!("hide_on_screenshot" in current)) return;
  delete current.hide_on_screenshot;
  writeFrontendSettings(current);
};
