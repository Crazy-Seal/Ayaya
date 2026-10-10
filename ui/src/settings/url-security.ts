export type HttpEndpointClassification =
  | { kind: "safe"; normalizedUrl: string }
  | { kind: "insecure_http"; normalizedUrl: string }
  | { kind: "invalid"; normalizedUrl: null };

/**
 * 确认 IPv4 地址四段有效且属于本机回环网段。
 *
 * Args:
 *     hostname (string): 待检查是否属于本机回环地址的主机名。
 *
 * Returns:
 *     boolean: 合法的 IPv4 回环地址为 true。
 */
const isIpv4Loopback = (hostname: string): boolean => {
  const parts = hostname.split(".");
  if (parts.length !== 4 || parts.some(/**
   * 拒绝 IPv4 各段中的非数字字符，避免不合法地址误当本机回环。
   *
   * Args:
   *     part (string): 模型资源路径中的一个片段。
   *
   * Returns:
   *     boolean: 该片段含非数字字符时为 true。
   */
  (part) => !/^\d+$/.test(part))) {
    return false;
  }
  const octets = parts.map(Number);
  return octets.every(/**
   * 检查 IPv4 数值片段是否位于零到二百五十五之间。
   *
   * Args:
   *     octet (number): IPv4 地址中的一个数值片段。
   *
   * Returns:
   *     boolean: 当前片段合法时为 true。
   */
  (octet) => octet >= 0 && octet <= 255) && octets[0] === 127;
};

/**
 * 识别本机回环主机名及 IPv4、IPv6 地址。
 *
 * Args:
 *     hostname (string): 待检查是否属于本机回环地址的主机名。
 *
 * Returns:
 *     boolean: 地址属于本机时为 true。
 */
const isLoopbackHostname = (hostname: string): boolean => {
  const normalized = hostname.toLowerCase();
  return normalized === "localhost" || normalized === "[::1]" || normalized === "::1" || isIpv4Loopback(normalized);
};

/**
 * 解析模型或 MCP 服务地址，区分 HTTPS、本机 HTTP 和远程明文 HTTP。
 *
 * Args:
 *     rawUrl (string): 调用方提供的原始页面地址。
 *
 * Returns:
 *     HttpEndpointClassification: 地址分类与校验结果。
 */
export const classifyHttpEndpoint = (rawUrl: string): HttpEndpointClassification => {
  let parsed: URL;
  try {
    parsed = new URL(rawUrl.trim());
  } catch {
    return { kind: "invalid", normalizedUrl: null };
  }

  if (parsed.username || parsed.password || (parsed.protocol !== "http:" && parsed.protocol !== "https:")) {
    return { kind: "invalid", normalizedUrl: null };
  }

  const normalizedUrl = parsed.pathname === "/" && !parsed.search && !parsed.hash
    ? parsed.href.slice(0, -1)
    : parsed.href;
  if (parsed.protocol === "https:" || isLoopbackHostname(parsed.hostname)) {
    return { kind: "safe", normalizedUrl };
  }
  return { kind: "insecure_http", normalizedUrl };
};

type AuthorizeBaseUrlChangeOptions = {
  previousUrl: string;
  nextUrl: string;
  confirmInsecure: (normalizedUrl: string) => Promise<boolean>;
};

/**
 * 保存地址前检查新地址类型；远程 HTTP 需要等待用户确认。
 *
 * Args:
 *     previousUrl (string): 该配置之前保存的服务地址。
 *     nextUrl (string): 用户准备保存的新服务地址。
 *     confirmInsecure ((normalizedUrl: string) => Promise<boolean>): 新地址需要确认时向用户展示提示并等待选择的函数。
 *
 * Returns:
 *     Promise<boolean>: 新地址可以保存时为 true。
 */
export const authorizeBaseUrlChange = async ({
  previousUrl,
  nextUrl,
  confirmInsecure,
}: AuthorizeBaseUrlChangeOptions): Promise<boolean> => {
  if (nextUrl.trim() === "") {
    return true;
  }

  const next = classifyHttpEndpoint(nextUrl);
  if (next.kind === "invalid") {
    throw new Error("Base URL 必须是有效的 HTTP 或 HTTPS 地址");
  }

  if (next.kind !== "insecure_http") {
    return true;
  }

  const previous = classifyHttpEndpoint(previousUrl);
  if (previous.kind !== "invalid" && previous.normalizedUrl === next.normalizedUrl) {
    return true;
  }

  return confirmInsecure(next.normalizedUrl);
};
