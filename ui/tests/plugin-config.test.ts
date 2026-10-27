import assert from "node:assert/strict";
import { test } from "node:test";
import { validatePluginConfig } from "../src/settings/plugin-config.js";

test("插件配置前端校验覆盖嵌套字段、枚举和类型，且不修改输入", /** 验证 JSON 编辑内容使用完整 Schema。 */ () => {
  const schema = { type: "object", required: ["endpoint"], properties: {
    endpoint: { type: "string", minLength: 1 },
    nested: { type: "object", properties: { count: { type: "integer", minimum: 1 } } },
    mode: { enum: ["one", "two"] },
  } };
  const config = { endpoint: "", nested: { count: "0" }, mode: "invalid" };
  const before = structuredClone(config);
  const errors = validatePluginConfig(schema, config, true);
  assert.deepEqual(errors.map(/** 取出各错误对应的参数路径。 */ (error) => error.path).sort(), ["/endpoint", "/mode", "/nested/count"]);
  assert.deepEqual(config, before);
  assert.equal(validatePluginConfig(schema, {}, false).length, 0);
  assert.equal(validatePluginConfig(schema, {}, true)[0].path, "/endpoint");
});

test("禁用插件允许可空嵌套对象和数组暂缺必填项，重新启用仍要求完整", /** 验证引用定义内的必填规则随插件开关切换，原参数及 Schema 保持原样。 */ () => {
  const schema = {
    $id: "https://ayaya.local/tests/nullable-plugin-config",
    type: "object",
    $defs: {
      Connection: { type: "object", required: ["endpoint"], properties: {
        endpoint: { type: "string", minLength: 2 },
        retries: { type: "integer", minimum: 1 },
      } },
    },
    properties: {
      connection: { anyOf: [{ $ref: "#/$defs/Connection" }, { type: "null" }], default: null },
      connections: { type: "array", items: { $ref: "#/$defs/Connection" } },
    },
  };
  const config = { connection: {}, connections: [{}] };
  const beforeSchema = structuredClone(schema);
  const beforeConfig = structuredClone(config);
  assert.deepEqual(validatePluginConfig(schema, config, false), []);
  const errors = validatePluginConfig(schema, config, true);
  assert.ok(errors.some(/** 确认可空对象在启用时要求填写地址。 */ (error) => error.path === "/connection/endpoint"));
  assert.ok(errors.some(/** 确认数组中的对象在启用时要求填写地址。 */ (error) => error.path === "/connections/0/endpoint"));
  assert.deepEqual(validatePluginConfig(schema, config, false), []);
  assert.deepEqual(validatePluginConfig(schema, { connection: null, connections: [{ endpoint: "game" }] }, true), []);
  assert.deepEqual(schema, beforeSchema);
  assert.deepEqual(config, beforeConfig);

  for (const connection of [{ endpoint: 42 }, { endpoint: "x" }, { retries: 0 }, { retries: "1" }]) {
    assert.ok(validatePluginConfig(schema, { connection }, false).length > 0);
  }
});

test("放宽必填规则保留同名字段和常量数据的校验", /** 防止递归处理把名为 required 的用户字段或 const 内容当作 Schema 关键字删除。 */ () => {
  const schema = { type: "object", required: ["required"], properties: {
    required: { type: "string" },
    fixed: { const: { required: ["endpoint"] } },
  } };
  assert.deepEqual(validatePluginConfig(schema, {}, false), []);
  assert.deepEqual(validatePluginConfig(schema, { required: "value", fixed: { required: ["endpoint"] } }, false), []);
  assert.equal(validatePluginConfig(schema, { required: 42 }, false)[0].path, "/required");
  assert.equal(validatePluginConfig(schema, { fixed: {} }, false)[0].path, "/fixed");
});
