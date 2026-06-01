/**
 * 共享类型定义
 * 供主进程、主窗口、设置窗口共同使用
 */

/**
 * 动作设置类型
 */
export type MotionSettingType = 'idle' | 'expression' | 'none';

/**
 * 动作配置
 */
export type MotionConfig = {
  motionName: string;
  setting: MotionSettingType;
  label?: string;
};
