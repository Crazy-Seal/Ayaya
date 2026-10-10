export type BlockedNavigationKind = "导航" | "新窗口";

type NavigationEvent = {
  /**
   * 约定取消当前页面导航的事件接口。
   */
  preventDefault(): void;
};

type GuardedWebContents = {
  /**
   * 约定注册页面导航事件的接口。
   *
   * Args:
   *     event ("will-navigate" | "will-redirect"): 调用方或浏览器触发的当前事件。
   *     listener ((event: NavigationEvent, targetUrl: string) => void): 接收后续事件的回调，取消订阅后不再调用。
   */
  on(
    event: "will-navigate" | "will-redirect",
    listener: (event: NavigationEvent, targetUrl: string) => void
  ): unknown;
  /**
   * 约定设置新窗口打开处理规则的接口。
   *
   * Args:
   *     handler ((details: { url: string }) => { action: "deny" }): 来源验证成功后执行的 IPC 处理函数。
   */
  setWindowOpenHandler(
    handler: (details: { url: string }) => { action: "deny" }
  ): unknown;
};

/**
 * 为应用窗口安装页面导航与新窗口限制，只放行可信页面地址。
 *
 * Args:
 *     webContents (GuardedWebContents): 需要安装访问限制的 Electron 页面对象。
 *     isAllowed ((targetUrl: string) => boolean): 按应用来源规则检查目标地址的函数。
 *     onBlocked ((kind: BlockedNavigationKind, targetUrl: string) => void): 导航被阻止后接收事件类型与目标地址的回调。
 */
export const installNavigationGuards = (
  webContents: GuardedWebContents,
  isAllowed: (targetUrl: string) => boolean,
  onBlocked: (kind: BlockedNavigationKind, targetUrl: string) => void
): void => {
  /**
   * 导航事件到达时校验目标地址，无效则取消并通知日志回调。
   *
   * Args:
   *     event (NavigationEvent): 调用方或浏览器触发的当前事件。
   *     targetUrl (string): 本次导航准备访问的目标地址。
   */
  const guardNavigation = (event: NavigationEvent, targetUrl: string): void => {
    if (!isAllowed(targetUrl)) {
      event.preventDefault();
      onBlocked("导航", targetUrl);
    }
  };
  webContents.on("will-navigate", guardNavigation);
  webContents.on("will-redirect", guardNavigation);
  webContents.setWindowOpenHandler(/**
   * 页面请求新窗口时记录目标地址并明确拒绝，防止应用页面打开未管理窗口。
   *
   * Args:
   *     url (string): 待检查或打开的地址。
   *
   * Returns:
   *     { action: "deny"; }: action 为 deny 的新窗口处理结果。
   */
  ({ url }) => {
    onBlocked("新窗口", url);
    return { action: "deny" };
  });
};
