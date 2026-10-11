/** 前端窗口与设置页面共用同一配置入口，窗口保存不会覆盖截图设置。 */
import fs from "node:fs";
import path from "node:path";
import { FRONTEND_SETTINGS_PATH } from "./config.js";
import type { FrontendSettings } from "../shared-types.js";

/**
 * 读取本地前端配置，缺失或损坏时使用默认截图设置。
 * Returns:
 *     FrontendSettings: 当前前端设置。
 */
export const readFrontendSettings = (): FrontendSettings => {
  try {
    return { hide_on_screenshot: true, ...JSON.parse(fs.readFileSync(FRONTEND_SETTINGS_PATH, "utf8")) };
  } catch { return { hide_on_screenshot: true }; }
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
  fs.mkdirSync(path.dirname(FRONTEND_SETTINGS_PATH), { recursive: true });
  // 2. 同目录临时文件写完后替换，异常时保留原文件。
  const temporary = `${FRONTEND_SETTINGS_PATH}.tmp`;
  fs.writeFileSync(temporary, JSON.stringify(settings, null, 2), "utf8");
  fs.renameSync(temporary, FRONTEND_SETTINGS_PATH);
  return settings;
};
