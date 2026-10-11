/** 使用统一窗口组件展示历史或草稿图片，来源由主进程校验。 */
import "./image-preview.css";
import { initializeIcons } from "./icons.js";
import { setupApplicationShutdownUi } from "./application-shutdown.js";

setupApplicationShutdownUi();
initializeIcons();
const image = document.getElementById("preview-image") as HTMLImageElement;
image.src = new URLSearchParams(window.location.search).get("image") ?? "";
image.addEventListener("error", /** 图片无法读取时展示错误，避免留下损坏图片图标。 */
() => { image.hidden = true; document.getElementById("preview-error")!.hidden = false; });
document.getElementById("preview-close-btn")!.addEventListener("click", /** 关闭图片窗口不影响聊天会话。 */
() => window.desktopPetApi.closeCurrentWindow());
window.addEventListener("keydown", /**
 * Escape 关闭预览，保留原有快捷键。
 * Args:
 *     event (KeyboardEvent): 当前预览窗口的按键事件。
 */
(event) => { if (event.key === "Escape") window.desktopPetApi.closeCurrentWindow(); });
