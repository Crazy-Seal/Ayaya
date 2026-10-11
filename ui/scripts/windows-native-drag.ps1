param(
  [long]$NativeHandle,
  [long]$NativePetHandle,
  [ValidateSet('left','top','right','bottom','overlap')][string]$Mode,
  [double]$Scale,
  [int]$AreaX, [int]$AreaY, [int]$AreaWidth, [int]$AreaHeight,
  [string]$OutputDirectory
)

# 1. 使用独立进程发送真实鼠标操作，避免 Electron 的原生拖动循环阻塞测试调度。
Add-Type -ReferencedAssemblies System.Drawing -TypeDefinition @'
using System;
using System.Drawing;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;
using System.Threading;

/** 在隔离测试桌面上执行鼠标拖动并采样原生屏幕，不修改应用业务数据。 */
public static class NativeDragProbe {
  private static string currentDirectory;
  /** 保存 user32 返回的物理屏幕矩形。 */
  [StructLayout(LayoutKind.Sequential)] public struct Rect { public int Left, Top, Right, Bottom; }
  /** 保存 user32 返回的物理屏幕鼠标坐标。 */
  [StructLayout(LayoutKind.Sequential)] public struct Point { public int X, Y; }
  /** 读取已隔离聊天窗口的系统矩形。 */
  [DllImport("user32.dll")] private static extern bool GetWindowRect(IntPtr window, out Rect rect);
  /** 移动实际系统鼠标，坐标使用物理像素。 */
  [DllImport("user32.dll")] private static extern bool SetCursorPos(int x, int y);
  /** 保存测试开始前的鼠标位置，以便 finally 恢复。 */
  [DllImport("user32.dll")] private static extern bool GetCursorPos(out Point point);
  /** 发送系统左键按下或松开，让应用执行实际拖动路径。 */
  [DllImport("user32.dll")] private static extern void mouse_event(uint flags, uint x, uint y, uint data, UIntPtr extra);
  /** 使用物理屏幕坐标，避免 Windows 的 125% 缩放把截图区域再次换算。 */
  [DllImport("user32.dll")] private static extern bool SetProcessDpiAwarenessContext(IntPtr context);
  /** 读取当前前台窗口，用于拒绝记录用户切换到其他应用后的画面。 */
  [DllImport("user32.dll")] private static extern IntPtr GetForegroundWindow();
  /** 读取窗口所属进程，确认前台仍属于隔离测试。 */
  [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr window, out uint processId);
  /** 读取角色透明窗口的系统样式，检查按住聊天期间是否保持鼠标穿透。 */
  [DllImport("user32.dll", EntryPoint="GetWindowLongPtrW")] private static extern IntPtr GetWindowLongPtr(IntPtr window, int index);

  /**
   * 在捕获屏幕前确认测试仍在前台，用户切换应用时停止并恢复鼠标。
   * Args:
   *     processId (uint): 当前聊天窗口所属进程。
   * Raises:
   *     InvalidOperationException: 前台窗口已经属于其他应用，拒绝记录其画面。
   */
  private static void RequireTestForeground(uint processId) {
    uint foreground; GetWindowThreadProcessId(GetForegroundWindow(), out foreground);
    if (foreground != processId) throw new InvalidOperationException("测试已失去前台，停止采样以避免记录其他应用。");
  }

  /** 记录每种拖动模式的画面差异和实际窗口位置。 */
  public class Result {
    public string mode;
    public int whiteBaseline, whitePeak, ghostPeak, oppositeDrift, draggedDistance, pointerInterceptions, samples;
  }

  /**
   * 捕获本应用纯色工作区，并统计目标边带和左侧区域的异常像素。
   * Args:
   *     rect (Rect): 当前窗口实际系统矩形。
   *     mode (string): 当前被拖动的边或移动模式。
   *     area (Rect): 由本应用测试背景覆盖的屏幕区域。
   *     output (string): 可选的当前帧输出文件。
   * Returns:
   *     int[]: 白色边带与非背景像素数量。
   */
  private static int[] Sample(Rect rect, string mode, Rect area, string output) {
    using (var image = new Bitmap(area.Right - area.Left, area.Bottom - area.Top, PixelFormat.Format32bppArgb)) {
      using (var drawing = Graphics.FromImage(image)) {
        drawing.CopyFromScreen(area.Left, area.Top, 0, 0, image.Size, CopyPixelOperation.SourceCopy);
      }
      int white = 0, ghost = 0;
      int centerX = (rect.Left + rect.Right) / 2 - area.Left;
      int centerY = (rect.Top + rect.Bottom) / 2 - area.Top;
      int edge = mode == "left" ? rect.Right - area.Left : mode == "right" ? rect.Left - area.Left
        : mode == "top" ? rect.Bottom - area.Top : rect.Top - area.Top;
      if (mode != "overlap") {
        for (int along = -60; along < 60; along++) {
          for (int across = -55; across < 55; across++) {
            int x = mode == "left" || mode == "right" ? edge + across : centerX + along;
            int y = mode == "left" || mode == "right" ? centerY + along : edge + across;
            if (x < 0 || y < 0 || x >= image.Width || y >= image.Height) continue;
            var pixel = image.GetPixel(x, y);
            if (pixel.R > 220 && pixel.G > 220 && pixel.B > 220) white++;
          }
        }
      }
      for (int y = 30; y < image.Height - 30; y += 2) {
        for (int x = 8; x < 150; x += 2) {
          var pixel = image.GetPixel(x, y);
          if (Math.Abs(pixel.R - 21) > 5 || Math.Abs(pixel.G - 50) > 5 || Math.Abs(pixel.B - 76) > 5) ghost++;
        }
      }
      if (output != null || white > 80) image.Save(output ?? System.IO.Path.Combine(currentDirectory, mode + "-white.png"), ImageFormat.Png);
      return new int[] { white, ghost };
    }
  }

  /**
   * 拖动窗口原生边框或标题栏，期间连续记录对边与残影像素。
   * Args:
   *     handle (long): 已隔离聊天窗口的系统句柄。
   *     petHandle (long): 已隔离角色窗口的系统句柄。
   *     mode (string): 当前拖动场景。
   *     scale (double): 显示器缩放比例。
   *     areaX, areaY, areaWidth, areaHeight (int): 测试背景覆盖的物理屏幕区域。
   *     directory (string): 当前帧保存位置。
   * Returns:
   *     Result: 本次操作的最大画面异常与对边偏移。
   */
  public static Result Run(long handle, long petHandle, string mode, double scale, int areaX, int areaY, int areaWidth, int areaHeight, string directory) {
    SetProcessDpiAwarenessContext(new IntPtr(-4));
    currentDirectory = directory;
    var window = new IntPtr(handle); Rect initial; GetWindowRect(window, out initial);
    uint processId; GetWindowThreadProcessId(window, out processId); RequireTestForeground(processId);
    var area = new Rect { Left = areaX, Top = areaY, Right = areaX + areaWidth, Bottom = areaY + areaHeight };
    var result = new Result { mode = mode };
    var baseline = Sample(initial, mode, area, null); result.whiteBaseline = baseline[0];
    if (baseline[1] >= 100) throw new InvalidOperationException("测试背景被其他窗口遮挡，拒绝保存屏幕画面；请保持测试窗口位于前台。");
    int startX = mode == "left" ? initial.Left + 2 : mode == "right" ? initial.Right - 2 : (initial.Left + initial.Right) / 2;
    int startY = mode == "top" ? initial.Top + 2 : mode == "bottom" ? initial.Bottom - 2 : (initial.Top + initial.Bottom) / 2;
    if (mode == "overlap") { startX = initial.Left + (int)(80 * scale); startY = initial.Top + (int)(24 * scale); }
    Point original; GetCursorPos(out original);
    try {
      SetCursorPos(startX, startY); Thread.Sleep(100); mouse_event(2, 0, 0, 0, UIntPtr.Zero); Thread.Sleep(50);
      for (int step = 0; step < 40; step++) {
        int amount = mode == "overlap" ? (int)(step / 39.0 * 990 * scale) : (int)(Math.Sin(step / 39.0 * Math.PI * 2) * 90 * scale);
        int x = startX + (mode == "left" || mode == "right" || mode == "overlap" ? amount : 0);
        int y = startY + (mode == "overlap" ? (int)(step / 39.0 * 430 * scale) : mode == "top" || mode == "bottom" ? amount : 0);
        SetCursorPos(x, y); Thread.Sleep(16);
        Rect rect; GetWindowRect(window, out rect);
        RequireTestForeground(processId);
        if ((GetWindowLongPtr(new IntPtr(petHandle), -20).ToInt64() & 0x20) == 0) result.pointerInterceptions++;
        result.draggedDistance = Math.Max(result.draggedDistance, Math.Max(Math.Abs(rect.Left - initial.Left), Math.Max(Math.Abs(rect.Top - initial.Top), Math.Max(Math.Abs(rect.Right - initial.Right), Math.Abs(rect.Bottom - initial.Bottom)))));
        var pixels = Sample(rect, mode, area, step % 5 == 0 ? System.IO.Path.Combine(directory, mode + "-" + step + ".png") : null);
        result.whitePeak = Math.Max(result.whitePeak, pixels[0]); result.ghostPeak = Math.Max(result.ghostPeak, pixels[1]); result.samples++;
        int opposite = mode == "left" ? rect.Right - initial.Right : mode == "right" ? rect.Left - initial.Left
          : mode == "top" ? rect.Bottom - initial.Bottom : mode == "bottom" ? rect.Top - initial.Top : 0;
        result.oppositeDrift = Math.Max(result.oppositeDrift, Math.Abs(opposite));
      }
    } finally { mouse_event(4, 0, 0, 0, UIntPtr.Zero); Thread.Sleep(80); SetCursorPos(original.X, original.Y); }
    return result;
  }
}
'@

# 2. 输出仅含像素计数与测试窗口状态，截图只写入指定的隔离测试产物目录。
[NativeDragProbe]::Run($NativeHandle, $NativePetHandle, $Mode, $Scale, $AreaX, $AreaY, $AreaWidth, $AreaHeight, $OutputDirectory) | ConvertTo-Json -Compress
