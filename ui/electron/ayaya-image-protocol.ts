import { protocol } from "electron";

import { backendFetch } from "./backend-client.js";
import {
  buildImageBackendPath,
  normalizeImageRelativePath,
} from "./ayaya-image-path.js";

/** 初始化带认证代理的本地历史图片协议。 */
export const initAyayaImageProtocolHandler = (): void => {
  protocol.handle("ayaya-image", async (request) => {
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
