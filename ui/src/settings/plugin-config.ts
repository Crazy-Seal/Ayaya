import { Ajv2020 } from "ajv/dist/2020.js";
import { fullFormats } from "ajv-formats/dist/formats.js";
import type { ErrorObject, ValidateFunction } from "ajv";

/** JSON Schema 中用于控件选择的字段，完整约束交给校验器处理。 */
export type ConfigSchema = Record<string, unknown> & {
  properties?: Record<string, ConfigSchema>;
  $defs?: Record<string, ConfigSchema>;
  $ref?: string;
  title?: string;
  description?: string;
  type?: string | string[];
  enum?: unknown[];
  anyOf?: ConfigSchema[];
};

/** 定位插件配置中的一处错误。 */
export type ConfigError = { path: string; message: string };

const validatorOptions = { allErrors: true, strict: false, formats: fullFormats };
const ajv = new Ajv2020(validatorOptions);
// 同一 Schema 的两种必填规则使用独立注册表，保留原 $id 及引用时不会互相冲突。
const disabledAjv = new Ajv2020(validatorOptions);
const validators = new WeakMap<ConfigSchema, ValidateFunction>();
const disabledValidators = new WeakMap<ConfigSchema, ValidateFunction>();

/**
 * 为禁用插件生成允许暂缺必填参数的配置定义，保留已有参数的类型和取值约束。
 *
 * Args:
 *     schema (ConfigSchema): 当前配置或嵌套字段的定义。
 *
 * Returns:
 *     ConfigSchema: 移除各层 required 的独立定义，原 Schema 和默认值保持原样。
 */
function allowIncompleteConfig(schema: ConfigSchema): ConfigSchema {
  const result = { ...schema };
  // 1. 当前对象可以暂缺字段，已填写字段继续遵循原类型、枚举及数值等约束。
  delete result.required;
  // 2. 只处理存放子 Schema 的位置，字段名和 default、const 等配置数据保持原样。
  for (const key of ["properties", "patternProperties", "$defs", "definitions", "dependentSchemas"]) {
    const children = schema[key] as Record<string, ConfigSchema | boolean> | undefined;
    if (children) {
      result[key] = Object.fromEntries(Object.entries(children).map(/** 放宽每个嵌套字段或引用定义的必填要求。 */
        ([name, child]) => [name, typeof child === "boolean" ? child : allowIncompleteConfig(child)]));
    }
  }
  // 3. 可空字段、联合类型及数组中的对象也可以暂缺内部必填项。
  for (const key of ["anyOf", "allOf", "oneOf", "prefixItems"]) {
    const children = schema[key] as Array<ConfigSchema | boolean> | undefined;
    if (children) result[key] = children.map(/** 保留联合类型各分支的约束，只放宽必填要求。 */
      (child) => typeof child === "boolean" ? child : allowIncompleteConfig(child));
  }
  for (const key of ["items", "additionalProperties", "unevaluatedProperties", "contains", "not", "if", "then", "else", "propertyNames", "unevaluatedItems", "contentSchema"]) {
    const child = schema[key];
    if (typeof child === "object" && child !== null) result[key] = allowIncompleteConfig(child as ConfigSchema);
  }
  return result;
}

/**
 * 根据插件开关校验配置，禁用时允许暂缺必填项，保持用户输入类型和值不变。
 *
 * Args:
 *     schema (ConfigSchema): 插件提供的完整配置定义。
 *     config (Record<string, unknown>): 从控件和 JSON 编辑器读取的参数。
 *     enabled (boolean): 开启时要求所有必填字段完整。
 *
 * Returns:
 *     ConfigError[]: 可定位到字段的校验错误。
 */
export function validatePluginConfig(schema: ConfigSchema, config: Record<string, unknown>, enabled: boolean): ConfigError[] {
  // 1. 启用和禁用分别缓存校验函数，切换开关后使用对应的必填规则。
  const cache = enabled ? validators : disabledValidators;
  let validate = cache.get(schema);
  if (!validate) {
    validate = enabled ? ajv.compile(schema) : disabledAjv.compile(allowIncompleteConfig(schema));
    cache.set(schema, validate);
  }
  // 2. 让校验器按完整分支判断可空或联合类型，直接显示实际未通过的约束。
  validate(config);
  return (validate.errors || []).map(/** 将校验器路径转为字段错误。 */
    (error: ErrorObject) => {
      const suffix = error.keyword === "required" ? `/${error.params.missingProperty}` : "";
      return { path: error.instancePath + suffix, message: `参数不符合配置要求：${error.message || error.keyword}` };
    });
}

/**
 * 展开本地字段引用，保留字段自己的标题、说明及默认值。
 *
 * Args:
 *     schema (ConfigSchema): 当前字段定义。
 *     root (ConfigSchema): 包含引用定义的根 Schema。
 *
 * Returns:
 *     ConfigSchema: 供控件选择使用的字段定义。
 */
export function resolveConfigField(schema: ConfigSchema, root: ConfigSchema): ConfigSchema {
  if (!schema.$ref?.startsWith("#/$defs/")) return schema;
  const name = schema.$ref.slice("#/$defs/".length).replace(/~1/g, "/").replace(/~0/g, "~");
  return { ...root.$defs?.[name], ...schema };
}

/** 保存一个字段的编辑控件及读取方法。 */
type FieldBinding = { key: string; error: HTMLElement; read: () => unknown; present: () => boolean };

/** 使用 Schema 创建控件，并在保存时统一读取和显示字段错误。 */
export class PluginConfigForm {
  private bindings: FieldBinding[] = [];
  private original: Record<string, unknown>;
  private generalError?: HTMLElement;

  /**
   * 保存完整配置，控件只修改已编辑的字段。
   *
   * Args:
   *     schema (ConfigSchema): 后端返回的配置定义。
   *     config (Record<string, unknown>): 默认值与已保存值合并后的配置。
   */
  constructor(private schema: ConfigSchema, config: Record<string, unknown>) {
    this.original = structuredClone(config);
  }

  /**
   * 生成基础控件、可空开关和复杂字段 JSON 编辑区。
   *
   * Returns:
   *     HTMLElement: 插件卡片中的参数区域。
   */
  render(): HTMLElement {
    const body = document.createElement("div");
    body.className = "plugin-config-grid";
    this.bindings = [];
    this.generalError = document.createElement("div");
    this.generalError.className = "plugin-config-error plugin-config-summary";
    this.generalError.hidden = true;
    body.appendChild(this.generalError);
    for (const [key, raw] of Object.entries(this.schema.properties || {})) {
      // 1. 展开字段引用和可空分支，基础类型生成控件，其他结构交给 JSON 编辑区。
      const field = resolveConfigField(raw, this.schema);
      const variants = field.anyOf?.map(/** 展开可空分支中的本地引用。 */
        (variant) => resolveConfigField(variant, this.schema));
      const nonNull = variants?.filter(/** 识别唯一的非空基础类型。 */ (variant) => variant.type !== "null");
      const nullable = variants?.some(/** 检查字段是否允许显式空值。 */ (variant) => variant.type === "null") ||
        (Array.isArray(field.type) && field.type.includes("null"));
      const shape = nonNull?.length === 1 ? nonNull[0] : field;
      const type = Array.isArray(shape.type) ? shape.type.find(/** 选择可空字段的实际输入类型。 */ (value) => value !== "null") : shape.type;
      const simple = !shape.anyOf && ["boolean", "integer", "number", "string"].includes(type || "");
      const enumeration = shape.enum;
      const wrapper = document.createElement("div");
      wrapper.className = "plugin-config-field";
      if (!simple && !enumeration) wrapper.className += " plugin-config-field--json";
      const heading = document.createElement("div");
      heading.className = "plugin-config-field-heading";
      const label = document.createElement("label");
      label.textContent = field.title || key;
      const input = document.createElement(enumeration ? "select" : simple ? "input" : "textarea") as HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement;
      input.className = "ui-control ui-control--small";
      input.id = `plugin-field-${crypto.randomUUID()}`;
      label.htmlFor = input.id;
      input.dataset.configKey = key;
      const value = this.original[key];
      // 2. 用原参数初始化控件；未填写数字保持空白，布尔字段等待用户实际操作。
      let touched = false;
      input.addEventListener("input", /** 记录用户主动编辑，避免为未填写布尔字段虚构默认值。 */ () => { touched = true; });
      if (enumeration) {
        const empty = document.createElement("option");
        empty.value = "";
        empty.textContent = "请选择";
        input.appendChild(empty);
        for (const choice of enumeration) {
          const option = document.createElement("option");
          option.value = JSON.stringify(choice);
          option.textContent = String(choice);
          input.appendChild(option);
        }
        input.value = value === undefined ? "" : JSON.stringify(value);
      } else if (simple) {
        const control = input as HTMLInputElement;
        control.type = type === "boolean" ? "checkbox" : type === "string" ? "text" : "number";
        if (type === "boolean") control.className = "ui-checkbox";
        if (type === "boolean") control.checked = value === true;
        else control.value = value === undefined || value === null ? "" : String(value);
        if (type === "integer" || type === "number") {
          control.step = type === "integer" ? "1" : "any";
          if (typeof shape.minimum === "number") control.min = String(shape.minimum);
          if (typeof shape.maximum === "number") control.max = String(shape.maximum);
        }
      } else {
        input.value = value === undefined ? "" : JSON.stringify(value, null, 2);
      }
      // 3. 布尔开关与标题同行，其他输入放在标题下方，保持每个参数独立成组。
      heading.appendChild(label);
      if (simple && type === "boolean" && !enumeration) heading.appendChild(input);
      wrapper.appendChild(heading);
      if (!(simple && type === "boolean" && !enumeration)) wrapper.appendChild(input);
      const nullInput = document.createElement("input");
      // 4. 显式 null 与尚未填写分别保存，切换空值时只禁用对应输入控件。
      nullInput.type = "checkbox";
      nullInput.className = "ui-checkbox";
      nullInput.checked = value === null;
      if (nullable) {
        const nullLabel = document.createElement("label");
        nullLabel.className = "plugin-config-null";
        nullLabel.textContent = "使用空值 null";
        nullInput.addEventListener("change", /** 切换空值时明确记录修改并更新控件状态。 */ () => {
          touched = true;
          input.disabled = nullInput.checked;
        });
        input.disabled = nullInput.checked;
        nullLabel.appendChild(nullInput);
        wrapper.appendChild(nullLabel);
      }
      const description = document.createElement("small");
      description.className = "plugin-config-description";
      description.id = `${input.id}-description`;
      description.textContent = field.description || "";
      const details = document.createElement("small");
      details.className = "plugin-config-details";
      if (Object.hasOwn(field, "default")) details.textContent = `默认值：${JSON.stringify(field.default)}`;
      const error = document.createElement("div");
      // 5. 把字段说明和错误关联到编辑区，并登记保存时读取值的方法。
      error.className = "plugin-config-error";
      error.id = `${input.id}-error`;
      input.setAttribute("aria-describedby", `${description.id} ${error.id}`);
      wrapper.append(description, details, error);
      body.appendChild(wrapper);
      this.bindings.push({ key, error,
        present: /** 未填写布尔字段保持缺省，checkbox 的默认 value 不代表用户输入。 */ () => touched || Object.hasOwn(this.original, key) || (type !== "boolean" && input.value !== ""),
        read: /** 按声明读取值；空数字保持缺省，不转换为零。 */ () => {
          if (nullable && nullInput.checked) return null;
          if (enumeration) return input.value === "" ? undefined : JSON.parse(input.value);
          if (simple) {
            if (type === "boolean") return (input as HTMLInputElement).checked;
            if (type === "string") return input.value;
            return input.value === "" ? undefined : Number(input.value);
          }
          return input.value === "" ? undefined : JSON.parse(input.value);
        },
      });
    }
    return body;
  }

  /**
   * 读取全部参数并校验，将错误显示在对应编辑区。
   *
   * Args:
   *     enabled (boolean): 插件是否启用，决定必填规则。
   *
   * Returns:
   *     Record<string, unknown>: 已校验配置。
   *
   * Raises:
   *     Error: 输入不符合 Schema，保存应停止。
   */
  read(enabled: boolean): Record<string, unknown> {
    const config = structuredClone(this.original);
    const errors: ConfigError[] = [];
    // 1. 读取已编辑字段，先报告 JSON 语法错误，保留未编辑的其他参数。
    for (const binding of this.bindings) {
      binding.error.textContent = "";
      try {
        if (!binding.present()) continue;
        const value = binding.read();
        if (value === undefined) delete config[binding.key];
        else config[binding.key] = value;
      } catch {
        errors.push({ path: `/${binding.key}`, message: "请输入有效的 JSON" });
      }
    }
    // 2. 语法有效后校验全部 Schema 约束，有错误时展示并终止保存。
    if (!errors.length) errors.push(...validatePluginConfig(this.schema, config, enabled));
    this.showErrors(errors);
    if (errors.length) throw new Error("插件参数有误，请检查标出的字段");
    return config;
  }

  /**
   * 展示前端或后端返回的字段错误。
   *
   * Args:
   *     errors (ConfigError[]): 相对于当前插件配置的错误路径。
   */
  showErrors(errors: ConfigError[]): void {
    if (this.generalError) { this.generalError.textContent = ""; this.generalError.hidden = true; }
    for (const binding of this.bindings) binding.error.textContent = "";
    for (const error of errors) {
      const key = error.path.split("/")[1]?.replace(/~1/g, "/").replace(/~0/g, "~");
      const binding = this.bindings.find(/** 将嵌套字段错误显示在所属编辑区。 */ (item) => item.key === key);
      if (binding) binding.error.textContent += `${error.path}：${error.message} `;
      else if (this.generalError) {
        this.generalError.hidden = false;
        this.generalError.textContent += `${error.path}：${error.message} `;
      }
    }
  }
}
