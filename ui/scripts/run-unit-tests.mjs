import { readdirSync, existsSync } from "node:fs";
import { join, relative } from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const uiRoot = fileURLToPath(new URL("../", import.meta.url));
const sourceRoot = join(uiRoot, "tests");

const testSources = (directory) => readdirSync(directory, { withFileTypes: true })
  .flatMap((entry) => {
    const source = join(directory, entry.name);
    if (entry.isDirectory()) return testSources(source);
    return entry.isFile() && entry.name.endsWith(".test.ts") ? [source] : [];
  });

const tests = testSources(sourceRoot).map((source) =>
  join(uiRoot, ".test-dist", relative(uiRoot, source).replace(/\.ts$/, ".js"))
);
if (tests.length === 0 || tests.some((test) => !existsSync(test))) {
  throw new Error("单测源码为空或对应的编译产物不存在");
}

const result = spawnSync(process.execPath, ["--test", ...tests], { stdio: "inherit" });
if (result.error) throw result.error;
process.exitCode = result.status ?? 1;
