/**
 * Live2D 自定义协议处理
 */
import { AYAYA_IMAGE_SCHEME } from "../shared-types.js";

import { protocol, net } from "electron";
import path from "node:path";
import { pathToFileURL } from "node:url";

import { loadModelConfig } from "./model-manager.js";

/**
 * 注册 live2d:// 自定义协议
 */
export const registerLive2DProtocol = (): void => {
  protocol.registerSchemesAsPrivileged([
    {
      scheme: "live2d",
      privileges: {
        standard: true,
        secure: true,
        supportFetchAPI: true,
        corsEnabled: true,
      },
    },
    {
      scheme: AYAYA_IMAGE_SCHEME,
      privileges: {
        standard: true,
        secure: true,
        supportFetchAPI: true,
      },
    },
  ]);
};

/**
 * 处理 live2d:// 协议请求
 *
 * Args:
 *     request (Request): 当前收到的页面请求。
 */
export const handleLive2DProtocol = async (request: Request): Promise<Response> => {
  try {
    const requestUrl = new URL(request.url);
    const modelId = requestUrl.hostname;
    const relPath = decodeURIComponent(requestUrl.pathname).replace(/^\/+/, "");
    const config = loadModelConfig();
    const model = config.models.find(
      /**
       * 解析模型协议地址时只匹配指定编号的自定义模型，内置模型不进入自定义文件读取。
       *
       * Args:
       *     item (ModelRecord): 当前遍历的配置或目录条目。
       *
       * Returns:
       *     boolean: 编号及来源都匹配时为 true。
       */
      (item) => item.id === modelId && item.source === "custom"
    );
    if (!model?.rootDir) {
      return new Response("Model not found", { status: 404 });
    }

    const { resolveRootDirAbsolute } = await import("./model-manager.js");
    const rootDir = resolveRootDirAbsolute(model.rootDir);
    const absoluteFilePath = path.normalize(path.join(rootDir, relPath));
    const normalizedRoot = path.normalize(`${rootDir}${path.sep}`);
    if (!absoluteFilePath.startsWith(normalizedRoot)) {
      return new Response("Forbidden", { status: 403 });
    }

    return net.fetch(pathToFileURL(absoluteFilePath).toString());
  } catch {
    return new Response("Bad request", { status: 400 });
  }
};

/**
 * 初始化 Live2D 协议处理器
 */
export const initLive2DProtocolHandler = (): void => {
  protocol.handle("live2d", handleLive2DProtocol);
};
