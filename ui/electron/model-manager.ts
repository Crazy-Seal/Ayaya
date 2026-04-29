/**
 * Live2D 模型配置管理
 */

import fs from "node:fs";
import path from "node:path";
import { randomUUID } from "node:crypto";
import { fileURLToPath } from "node:url";

import {
  USER_DATA_DIR,
  IMPORTED_MODELS_DIR,
  LEGACY_IMPORTED_MODELS_DIR,
  MODEL_CONFIG_PATH,
} from "./config.js";
import type { ModelRecord, ModelConfig, ImportPreview } from "./types.js";

/**
 * 确保存储目录存在
 */
export const ensureStorage = (): void => {
  fs.mkdirSync(USER_DATA_DIR, { recursive: true });
  fs.mkdirSync(IMPORTED_MODELS_DIR, { recursive: true });
};

/**
 * 将绝对路径转换为相对于 UI_ROOT 的 POSIX 路径
 */
export const toPosixRelative = (absolutePath: string): string => {
  return path.relative(USER_DATA_DIR, absolutePath).replaceAll("\\", "/");
};

/**
 * 将绝对路径转换为相对于指定基目录的 POSIX 路径
 */
export const toPosixRelativeFrom = (baseDir: string, absolutePath: string): string => {
  return path.relative(baseDir, absolutePath).replaceAll("\\", "/");
};

// UI_ROOT 用于 resolveRootDirAbsolute，延迟计算避免循环依赖
let uiRoot: string | null = null;

const getUiRoot = (): string => {
  if (uiRoot === null) {
    // 计算相对于当前模块的 UI_ROOT
    uiRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
  }
  return uiRoot;
};

/**
 * 解析 rootDir 为绝对路径
 */
export const resolveRootDirAbsolute = (rootDir: string): string => {
  if (path.isAbsolute(rootDir)) {
    return rootDir;
  }

  const fromUserData = path.resolve(USER_DATA_DIR, rootDir);
  if (fs.existsSync(fromUserData)) {
    return fromUserData;
  }

  return path.resolve(getUiRoot(), rootDir);
};

/**
 * 清理模型名称中的非法字符
 */
export const sanitizeModelName = (name: string): string => {
  const cleaned = name.trim().replace(/[\\/:*?"<>|]/g, "_");
  return cleaned || `model_${Date.now()}`;
};

/**
 * 解析唯一的模型目录名
 */
export const resolveUniqueModelDir = (baseName: string): string => {
  const normalized = sanitizeModelName(baseName);
  let candidate = path.join(IMPORTED_MODELS_DIR, normalized);
  let index = 1;
  while (fs.existsSync(candidate)) {
    candidate = path.join(IMPORTED_MODELS_DIR, `${normalized}_${index}`);
    index += 1;
  }
  return candidate;
};

/**
 * 在目录中递归查找 .model3.json 文件
 */
export const findModel3JsonRelativePath = (rootDir: string): string | null => {
  const stack = [rootDir];
  while (stack.length > 0) {
    const current = stack.pop();
    if (!current) {
      continue;
    }

    for (const entry of fs.readdirSync(current, { withFileTypes: true })) {
      const absolutePath = path.join(current, entry.name);
      if (entry.isDirectory()) {
        stack.push(absolutePath);
        continue;
      }

      if (entry.isFile() && entry.name.endsWith(".model3.json")) {
        return path.relative(rootDir, absolutePath).replaceAll("\\", "/");
      }
    }
  }

  return null;
};

/**
 * 复制目录
 */
export const copyDirectory = (fromDir: string, toDir: string): void => {
  fs.cpSync(fromDir, toDir, { recursive: true, force: true });
};

/**
 * 检查导入源
 */
export const inspectImportSource = (selectedPath: string): ImportPreview => {
  const stat = fs.statSync(selectedPath);
  if (stat.isDirectory()) {
    const entryRelativePath = findModel3JsonRelativePath(selectedPath);
    if (!entryRelativePath) {
      throw new Error("Model3.json not found in the selected folder");
    }

    return {
      selectedPath,
      sourceType: "directory",
      suggestedName: sanitizeModelName(path.basename(selectedPath)),
      entryRelativePath,
    };
  }

  throw new Error("Please select a Live2D model folder");
};

/**
 * 创建默认模型配置
 */
export const createDefaultModelConfig = (): ModelConfig => {
  const builtinId = "builtin-hiyori";
  return {
    activeModelId: builtinId,
    models: [
      {
        id: builtinId,
        name: "hiyori_pro_t11",
        sessionId: randomUUID(),
        source: "builtin",
        entry: "/live2d/hiyori_pro_zh/runtime/hiyori_pro_t11.model3.json",
        offsetX: 0,
        offsetY: 0,
        userScale: 1,
        followCursor: true,
      },
    ],
  };
};

/**
 * 加载模型配置
 */
export const loadModelConfig = (): ModelConfig => {
  ensureStorage();
  if (!fs.existsSync(MODEL_CONFIG_PATH)) {
    const initial = createDefaultModelConfig();
    fs.writeFileSync(MODEL_CONFIG_PATH, JSON.stringify(initial, null, 2), "utf-8");
    return initial;
  }

  try {
    const raw = fs.readFileSync(MODEL_CONFIG_PATH, "utf-8");
    const parsed = JSON.parse(raw) as ModelConfig;
    let changed = false;

    for (const model of parsed.models ?? []) {
      if (model.source === "custom" && model.rootDir) {
        if (path.isAbsolute(model.rootDir)) {
          model.rootDir = toPosixRelative(model.rootDir);
          changed = true;
        }

        const modelRootAbs = resolveRootDirAbsolute(model.rootDir);
        const normalizedLegacyBase = path.normalize(`${LEGACY_IMPORTED_MODELS_DIR}${path.sep}`);
        const normalizedModelRoot = path.normalize(modelRootAbs);
        if (
          normalizedModelRoot.startsWith(normalizedLegacyBase) &&
          fs.existsSync(modelRootAbs)
        ) {
          const targetRoot = resolveUniqueModelDir(path.basename(modelRootAbs));
          copyDirectory(modelRootAbs, targetRoot);
          model.rootDir = toPosixRelativeFrom(USER_DATA_DIR, targetRoot);
          changed = true;
        }
      }

      if (typeof model.offsetX !== "number") {
        model.offsetX = 0;
        changed = true;
      }
      if (typeof model.offsetY !== "number") {
        model.offsetY = 0;
        changed = true;
      }
      if (typeof model.userScale !== "number") {
        model.userScale = 1;
        changed = true;
      }
      if (typeof model.followCursor !== "boolean") {
        model.followCursor = true;
        changed = true;
      }
    }

    if (!Array.isArray(parsed.models) || parsed.models.length === 0) {
      const initial = createDefaultModelConfig();
      fs.writeFileSync(MODEL_CONFIG_PATH, JSON.stringify(initial, null, 2), "utf-8");
      return initial;
    }

    if (!parsed.models.some((item) => item.id === parsed.activeModelId)) {
      parsed.activeModelId = parsed.models[0].id;
      changed = true;
    }

    if (changed) {
      fs.writeFileSync(MODEL_CONFIG_PATH, JSON.stringify(parsed, null, 2), "utf-8");
    }

    return parsed;
  } catch {
    const initial = createDefaultModelConfig();
    fs.writeFileSync(MODEL_CONFIG_PATH, JSON.stringify(initial, null, 2), "utf-8");
    return initial;
  }
};

/**
 * 保存模型配置
 */
export const saveModelConfig = (config: ModelConfig): void => {
  ensureStorage();
  fs.writeFileSync(MODEL_CONFIG_PATH, JSON.stringify(config, null, 2), "utf-8");
};

/**
 * 获取当前激活的模型记录
 */
export const getActiveModelRecord = (): ModelRecord => {
  const config = loadModelConfig();
  const found = config.models.find((item) => item.id === config.activeModelId);
  return found ?? config.models[0];
};

/**
 * 解析模型 URL
 */
export const resolveModelUrl = (model: ModelRecord): string => {
  if (model.source === "builtin") {
    return model.entry;
  }

  const encodedEntry = model.entry
    .replaceAll("\\", "/")
    .split("/")
    .map((part) => encodeURIComponent(part))
    .join("/");
  return `live2d://${model.id}/${encodedEntry}`;
};

/**
 * 创建新的模型记录
 */
export const createModelRecord = (
  modelName: string,
  destDir: string,
  entryName: string
): ModelRecord => {
  const modelId = `model-${randomUUID()}`;
  return {
    id: modelId,
    name: path.basename(destDir),
    sessionId: randomUUID(),
    source: "custom",
    entry: entryName,
    rootDir: toPosixRelativeFrom(USER_DATA_DIR, destDir),
    offsetX: 0,
    offsetY: 0,
    userScale: 1,
    followCursor: true,
  };
};
