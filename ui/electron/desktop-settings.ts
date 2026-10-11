/** 后端桌面 YAML 的主进程读写入口，成功迁移后只移除旧 JSON 的截图字段。 */
import { backendFetch } from "./backend-client.js";
import { removeLegacyScreenshotSetting } from "./frontend-settings-store.js";
import { applicationOperations } from "./application-operations.js";
import type { DesktopSettings } from "../shared-types.js";

/**
 * 读取或更新后端配置，退出后的迟到响应不能再改写前端文件。
 * Args:
 *     settings (DesktopSettings | undefined): 提供时保存，否则读取。
 * Returns:
 *     Promise<DesktopSettings>: 后端确认保存的配置。
 */
const request = async (settings?: DesktopSettings): Promise<DesktopSettings> => {
  if (applicationOperations.isShuttingDown()) throw new Error("应用正在退出");
  const response = await backendFetch("/desktop_settings", settings ? {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(settings),
  } : {});
  if (!response.ok) throw new Error(`桌面设置请求失败: ${response.status}`);
  const result = await response.json() as { code: number; data: DesktopSettings };
  if (result.code !== 200 || typeof result.data?.hide_on_screenshot !== "boolean") {
    throw new Error("后端没有确认桌面设置迁移成功");
  }
  if (applicationOperations.isShuttingDown()) throw new Error("应用正在退出");
  removeLegacyScreenshotSetting();
  return result.data;
};

/**
 * 返回已完成迁移的后端桌面设置。
 * Returns:
 *     Promise<DesktopSettings>: 已写入 YAML 的全局设置。
 */
export const getDesktopSettings = (): Promise<DesktopSettings> => request();
/**
 * 保存设置并返回实际写入结果。
 * Args:
 *     settings (DesktopSettings): 已编辑的完整桌面显隐设置。
 * Returns:
 *     Promise<DesktopSettings>: 后端确认原子保存的值。
 */
export const updateDesktopSettings = (settings: DesktopSettings): Promise<DesktopSettings> => request(settings);
