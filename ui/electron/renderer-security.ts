export type TrustedRendererPolicy =
  | {
      mode: "development";
      origin: string;
      allowedPaths: ReadonlySet<string>;
    }
  | {
      mode: "production";
      allowedUrls: ReadonlySet<string>;
    };

type TrustedRendererPolicyOptions = {
  devServerUrl?: string;
  productionEntryUrls: string[];
};

type IpcSenderDescriptor = {
  url: string;
  isMainFrame: boolean;
};

const DEV_ALLOWED_PATHS = new Set(["/", "/index.html", "/settings.html", "/logs.html"]);

/**
 * 确认 IPv4 地址格式合法且属于 127 回环网段。
 *
 * Args:
 *     hostname (string): 待检查是否属于本机回环地址的主机名。
 *
 * Returns:
 *     boolean: 有效 IPv4 回环地址为 true。
 */
const isIpv4Loopback = (hostname: string): boolean => {
  const parts = hostname.split(".");
  if (parts.length !== 4 || parts.some(/**
   * 检查 IPv4 各段是否包含非数字内容，非法片段导致整个回环地址判断失败。
   *
   * Args:
   *     part (string): 模型资源路径中的一个片段。
   *
   * Returns:
   *     boolean: 当前片段含非数字字符时为 true。
   */
  (part) => !/^\d+$/.test(part))) {
    return false;
  }
  const octets = parts.map(Number);
  return octets.every(/**
   * 确认 IPv4 数值片段在零到二百五十五之间。
   *
   * Args:
   *     octet (number): IPv4 地址中的一个数值片段。
   *
   * Returns:
   *     boolean: 当前片段在 IPv4 范围内时为 true。
   */
  (octet) => octet >= 0 && octet <= 255) && octets[0] === 127;
};

/**
 * 识别 localhost、IPv4 和 IPv6 本机回环名称。
 *
 * Args:
 *     hostname (string): 待检查是否属于本机回环地址的主机名。
 *
 * Returns:
 *     boolean: 主机名属于本机回环地址时为 true。
 */
const isLoopbackHostname = (hostname: string): boolean => {
  const normalized = hostname.toLowerCase();
  return normalized === "localhost" || normalized === "[::1]" || normalized === "::1" || isIpv4Loopback(normalized);
};

/**
 * 将生产页面地址规范化为可比较的 URL。
 *
 * Args:
 *     rawUrl (string): 调用方提供的原始页面地址。
 *
 * Returns:
 *     string | null: 规范化地址，无法解析时为空。
 */
const normalizedProductionUrl = (rawUrl: string): string | null => {
  try {
    const parsed = new URL(rawUrl);
    if (parsed.protocol !== "file:" || parsed.username || parsed.password || parsed.host) {
      return null;
    }
    parsed.search = "";
    parsed.hash = "";
    return parsed.href;
  } catch {
    return null;
  }
};

/**
 * 根据开发来源或生产入口文件生成应用页面访问规则。
 *
 * Args:
 *     devServerUrl (string | undefined): 开发环境允许加载的 Vite 页面来源。
 *     productionEntryUrls (string[]): 生产环境允许加载的本地页面地址。
 *
 * Returns:
 *     TrustedRendererPolicy: 供 IPC 与导航共同使用的可信页面规则。
 */
export const createTrustedRendererPolicy = ({
  devServerUrl,
  productionEntryUrls,
}: TrustedRendererPolicyOptions): TrustedRendererPolicy => {
  if (devServerUrl) {
    let parsed: URL;
    try {
      parsed = new URL(devServerUrl);
    } catch {
      throw new Error("VITE_DEV_SERVER_URL 必须是有效 URL");
    }
    if (
      (parsed.protocol !== "http:" && parsed.protocol !== "https:") ||
      parsed.username ||
      parsed.password ||
      !isLoopbackHostname(parsed.hostname)
    ) {
      throw new Error("VITE_DEV_SERVER_URL 必须使用环回地址");
    }
    if (parsed.pathname !== "/" || parsed.search || parsed.hash) {
      throw new Error("VITE_DEV_SERVER_URL 必须是纯 origin，不能包含路径、查询或片段");
    }
    return {
      mode: "development",
      origin: parsed.origin,
      allowedPaths: DEV_ALLOWED_PATHS,
    };
  }

  const allowedUrls = new Set<string>();
  for (const entryUrl of productionEntryUrls) {
    const normalized = normalizedProductionUrl(entryUrl);
    if (!normalized) {
      throw new Error("生产 renderer 入口必须是本地 file URL");
    }
    allowedUrls.add(normalized);
  }
  return { mode: "production", allowedUrls };
};

/**
 * 检查页面地址是否符合当前开发来源或生产文件规则。
 *
 * Args:
 *     rawUrl (string): 调用方提供的原始页面地址。
 *     policy (TrustedRendererPolicy): 允许访问的应用页面及来源规则。
 *
 * Returns:
 *     boolean: 地址属于可信应用入口时为 true。
 */
export const isTrustedRendererUrl = (rawUrl: string, policy: TrustedRendererPolicy): boolean => {
  try {
    const parsed = new URL(rawUrl);
    if (parsed.username || parsed.password) {
      return false;
    }
    if (policy.mode === "development") {
      return parsed.origin === policy.origin && policy.allowedPaths.has(parsed.pathname);
    }
    const normalized = normalizedProductionUrl(rawUrl);
    return normalized !== null && policy.allowedUrls.has(normalized);
  } catch {
    return false;
  }
};

/**
 * 同时检查 IPC 发送页面和框架来源，拒绝不可信页面或子框架请求。
 *
 * Args:
 *     sender (IpcSenderDescriptor | null): 发起 IPC 请求的窗口或页面描述。
 *     policy (TrustedRendererPolicy): 允许访问的应用页面及来源规则。
 *
 * Returns:
 *     boolean: 发送方符合可信页面和框架约束时为 true。
 */
export const isTrustedIpcSender = (
  sender: IpcSenderDescriptor | null,
  policy: TrustedRendererPolicy
): boolean => Boolean(sender?.isMainFrame && isTrustedRendererUrl(sender.url, policy));

/**
 * 生成可写日志的页面地址描述，避免泄漏 URL 中敏感内容。
 *
 * Args:
 *     rawUrl (string): 调用方提供的原始页面地址。
 *
 * Returns:
 *     string: 经过清理的地址描述。
 */
export const describeRendererUrl = (rawUrl: string): string => {
  try {
    const parsed = new URL(rawUrl);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:" && parsed.protocol !== "file:") {
      return parsed.protocol;
    }
    return `${parsed.protocol}//${parsed.host}${parsed.pathname}`;
  } catch {
    return "invalid-url";
  }
};
