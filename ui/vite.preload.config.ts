import { fileURLToPath } from "node:url";

import { defineConfig } from "vite";

export default defineConfig({
  build: {
    outDir: "dist-electron/electron",
    emptyOutDir: false,
    minify: false,
    lib: {
      entry: fileURLToPath(new URL("./electron/preload.ts", import.meta.url)),
      formats: ["cjs"],
      /**
       * 为 preload 构建产物指定固定文件名，供 Electron 窗口加载。
       *
       * Returns:
       *     string: preload 输出文件名称。
       */
      fileName: () => "preload.cjs",
    },
    rollupOptions: {
      external: ["electron"],
    },
  },
});
