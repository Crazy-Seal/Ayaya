/** 表示后端启动或 Python 环境查询已被应用关闭流程取消。 */
export class BackendStartupCancelledError extends Error {
  /**
   * 创建可由调用方按类型识别的启动取消错误。
   *
   * Args:
   *     message (string): 展示或记录的取消说明。
   */
  constructor(message = "后端启动已取消") {
    super(message);
    this.name = "BackendStartupCancelledError";
  }
}
