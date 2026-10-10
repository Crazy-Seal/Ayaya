import { readdirSync, existsSync } from "node:fs";
import { join, relative } from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const uiRoot = fileURLToPath(new URL("../", import.meta.url));
const sourceRoot = join(uiRoot, "tests");

/**
 * 发现已编译的单元测试文件，并组织成测试执行器使用的文件列表。
 *
 * Args:
 *     directory (any): 当前环境的日志保存目录。
 *
 * Returns:
 *     any: 按名称整理的测试文件路径集合。
 */
const testSources = (directory) => readdirSync(directory, { withFileTypes: true })
  .flatMap(/**
   * 遍历测试目录时递归进入子目录，只收集 .test.ts 文件。
   *
   * Args:
   *     entry (Dirent<string>): 当前发现的单元测试文件名称。
   *
   * Returns:
   *     any: 当前目录项对应的测试源码路径列表。
   */
  (entry) => {
    const source = join(directory, entry.name);
    if (entry.isDirectory()) return testSources(source);
    return entry.isFile() && entry.name.endsWith(".test.ts") ? [source] : [];
  });

const tests = testSources(sourceRoot).map(/**
 * 把测试源码相对路径换成 .test-dist 中的 JavaScript 文件路径。
 *
 * Args:
 *     source (any): 日志产生的来源。
 *
 * Returns:
 *     string: 单个已编译测试文件路径。
 */
(source) =>
  join(uiRoot, ".test-dist", relative(uiRoot, source).replace(/\.ts$/, ".js"))
);
if (tests.length === 0 || tests.some(/**
 * 执行前检查测试产物是否缺失，缺失时由入口明确报告尚未完成编译。
 *
 * Args:
 *     test (any): 当前需要转发给单元测试执行器的测试选项。
 *
 * Returns:
 *     boolean: 当前编译文件不存在时为 true。
 */
(test) => !existsSync(test))) {
  throw new Error("单测源码为空或对应的编译产物不存在");
}

const result = spawnSync(process.execPath, ["--test", ...tests], { stdio: "inherit" });
if (result.error) throw result.error;
process.exitCode = result.status ?? 1;
