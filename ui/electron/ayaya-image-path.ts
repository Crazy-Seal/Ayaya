const SAFE_FILENAME = /^[A-Za-z0-9][A-Za-z0-9._ -]*$/;
const ALLOWED_DIRECTORIES = new Set(["tool", "screenshot", "mcp"]);

/**
 *  校验并规范化历史图片相对路径。
 *
 * Args:
 *     value (string): 当前需要显示、比较或保存的值。
 */
export const normalizeImageRelativePath = (value: string): string | null => {
  if (!value || value.includes("\\") || value.includes("..") || value.startsWith("/")) {
    return null;
  }
  const parts = value.split("/");
  if (parts.length === 1) {
    return SAFE_FILENAME.test(parts[0]) ? parts[0] : null;
  }
  if (
    parts.length === 2
    && ALLOWED_DIRECTORIES.has(parts[0])
    && SAFE_FILENAME.test(parts[1])
  ) {
    return `${parts[0]}/${parts[1]}`;
  }
  return null;
};

/**
 *  为后端静态图片服务逐段编码安全相对路径。
 *
 * Args:
 *     value (string): 当前需要显示、比较或保存的值。
 */
export const buildImageBackendPath = (value: string): string => {
  const normalized = normalizeImageRelativePath(value);
  if (normalized === null) {
    throw new Error("非法历史图片路径");
  }
  return `/images/${normalized.split("/").map(encodeURIComponent).join("/")}`;
};
