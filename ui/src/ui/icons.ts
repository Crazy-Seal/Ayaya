import { initializeWindowTheme } from "./window-theme.js";

/** 全部应用窗口共用的线框 SVG 图标；正文和模型资源不经过此入口。 */
const linePaths = { check: "m5 12 4 4L19 6", close: "m6 6 12 12M18 6 6 18" } as const;
const paths = {
  minimize: '<path d="M5 12h14"/>',
  close: `<path d="${linePaths.close}"/>`,
  send: '<path d="m22 2-7 20-4-9-9-4 20-7ZM22 2 11 13"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
  image: '<rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8" cy="8" r="1"/><path d="m21 15-5-5L5 21"/>',
  settings: '<path d="m10 3-.7 3-2.8 1-2.7-.9-2 3.5L4 12l-2.2 2.4 2 3.5 2.7-.9 2.8 1 .7 3h4l.7-3 2.8-1 2.7.9 2-3.5L20 12l2.2-2.4-2-3.5-2.7.9-2.8-1L14 3Z"/><circle cx="12" cy="12" r="3"/>',
  pin: '<path d="m16 3 5 5-4 1-3 6-5-5 6-3 1-4ZM9 15l-6 6M7 8l9 9"/>',
  log: '<rect x="5" y="3" width="15" height="18" rx="2"/><path d="M9 8h7M9 12h7M9 16h4M3 7h3M3 12h3M3 17h3"/>',
  exit: '<path d="M10 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h5M9 12h12m-5-5 5 5-5 5"/>',
  eye: '<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>',
  check: `<path d="${linePaths.check}"/>`,
  warning: '<path d="m12 3 10 18H2L12 3ZM12 9v4M12 17h.01"/>',
  chevron: '<path d="m6 9 6 6 6-6"/>',
} as const;

/** 应用允许使用的功能图标名称。 */
export type IconName = keyof typeof paths;

/**
 * 向已有动态提示 SVG 提供同一资源中的路径，不重复维护提示图标。
 * Args:
 *     name ("check" | "close"): 成功或错误图标名称。
 * Returns:
 *     string: SVG path 的 d 属性。
 */
export const iconPathData = (name: keyof typeof linePaths): string => linePaths[name];

/**
 * 从固定资源生成 SVG，静态按钮、动态按钮与 CSS 图标共用同一画法。
 *
 * Args:
 *     name (IconName): 已登记的功能图标名称。
 * Returns:
 *     string: 不含外部资源和用户内容的 SVG 标记。
 */
export const iconMarkup = (name: IconName): string =>
  `<svg xmlns="http://www.w3.org/2000/svg" class="ui-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${paths[name]}</svg>`;

/**
 * 填充当前页面的静态图标，并向下拉框和复选框提供相同 SVG 资源。
 *
 * Args:
 *     root (ParentNode): 当前页面或新加入页面的控件容器。
 */
export const initializeIcons = (root: ParentNode = document): void => {
  initializeWindowTheme();
  for (const element of root.querySelectorAll<HTMLElement>("[data-icon]")) {
    const name = element.dataset.icon as IconName;
    if (Object.hasOwn(paths, name)) element.innerHTML = iconMarkup(name);
  }
  for (const name of ["chevron", "check"] as const) {
    // 1. SVG 背景资源不能继承外部 currentColor，因此读取组件正在使用的主题颜色。
    const color = getComputedStyle(document.documentElement).getPropertyValue(
      name === "check" ? "--color-on-primary" : "--color-text-accent",
    ).trim();
    document.documentElement.style.setProperty(
      `--icon-${name}`,
      `url("data:image/svg+xml,${encodeURIComponent(iconMarkup(name).replace("currentColor", color))}")`,
    );
  }
};
