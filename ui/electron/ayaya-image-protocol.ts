import { AYAYA_IMAGE_SCHEME } from "../shared-types.js";
import { protocol } from "electron";

import { backendFetch } from "./backend-client.js";
import {
  buildImageBackendPath,
  normalizeImageRelativePath,
} from "./ayaya-image-path.js";

/**
 *  初始化带认证代理的本地历史图片协议。
 */
export const initAyayaImageProtocolHandler = (): void => {
  protocol.handle(AYAYA_IMAGE_SCHEME, /**
   * 由 Electron 图片协议调用，校验 local 主机和受限相对路径后读取历史图片，无效路径明确拒绝。
   *
   * Args:
   *     request (Request): 当前收到的页面请求。
   */
  async (request) => {
    try {
      const url = new URL(request.url);
      const relativePath = normalizeImageRelativePath(
        decodeURIComponent(url.pathname).replace(/^\/+/, "")
      );
      if (url.hostname !== "local" || relativePath === null) {
        return new Response("Forbidden", { status: 403 });
      }
      return backendFetch(buildImageBackendPath(relativePath));
    } catch {
      return new Response("Bad request", { status: 400 });
    }
  });
};
