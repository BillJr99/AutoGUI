import { Buffer } from "node:buffer";
import { commandExists, execFile } from "../process.js";
import { DesktopError, type BackendCapabilities, type BackendLogger, type DesktopBackend, type ElementInfo, type Mark, type PlatformInfo, type Rect, type ScreenshotOptions, type ScreenshotResult, type WindowInfo } from "../types.js";
import { makeScreenshotResult, parseJsonArray, savePng } from "./common.js";
import { marksFromWindows } from "../som.js";

const mouseType = `
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class PiMouse {
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] public static extern bool GetCursorPos(out POINT lpPoint);
  [DllImport("user32.dll")] public static extern void mouse_event(uint flags, uint dx, uint dy, uint data, UIntPtr extraInfo);
  public struct POINT { public int X; public int Y; }
}
"@
`;

const windowType = `
Add-Type -TypeDefinition @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class PiWindow {
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int count);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT r);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
}
"@
`;

const keyModifiers: Record<string, string> = {
  ctrl: "^",
  control: "^",
  alt: "%",
  shift: "+",
};

const keySpecial: Record<string, string> = {
  enter: "{ENTER}",
  return: "{ENTER}",
  tab: "{TAB}",
  escape: "{ESC}",
  esc: "{ESC}",
  backspace: "{BACKSPACE}",
  delete: "{DELETE}",
  del: "{DEL}",
  home: "{HOME}",
  end: "{END}",
  pageup: "{PGUP}",
  pagedown: "{PGDN}",
  up: "{UP}",
  down: "{DOWN}",
  left: "{LEFT}",
  right: "{RIGHT}",
  space: " ",
  f1: "{F1}",
  f2: "{F2}",
  f3: "{F3}",
  f4: "{F4}",
  f5: "{F5}",
  f6: "{F6}",
  f7: "{F7}",
  f8: "{F8}",
  f9: "{F9}",
  f10: "{F10}",
  f11: "{F11}",
  f12: "{F12}",
};

function toSendKeys(keys: string[]): string {
  let modifiers = "";
  const body: string[] = [];
  for (const key of keys) {
    const lower = key.toLowerCase();
    if (keyModifiers[lower]) modifiers += keyModifiers[lower];
    else if (keySpecial[lower]) body.push(keySpecial[lower]);
    else if (key.length === 1) body.push("+^%~{}[]() ".includes(key) ? `{${key}}` : key);
    else body.push(`{${key.toUpperCase()}}`);
  }
  const joined = body.join("");
  if (!modifiers) return joined;
  if (modifiers.length > 1 || body.length > 1) return `${modifiers}(${joined})`;
  return `${modifiers}${joined}`;
}

export class PowerShellBackend implements DesktopBackend {
  readonly name: string;
  readonly capabilities: BackendCapabilities = {
    findElement: true,   // via UIAutomation .NET assembly
    richMarks: true,     // can include UIAutomation children of focused window
    nativeInput: true,   // SendInput via PInvoke
  };
  private executablePromise?: Promise<string>;

  constructor(readonly platform: PlatformInfo, private readonly logger?: BackendLogger) {
    this.name = platform.isWsl ? "wsl-powershell" : "windows-powershell";
  }

  async findElement(query: { name?: string; controlType?: string; windowTitle?: string; index?: number }, signal?: AbortSignal): Promise<ElementInfo> {
    const encoded = Buffer.from(JSON.stringify({
      name: query.name ?? "",
      controlType: query.controlType ?? "",
      windowTitle: query.windowTitle ?? "",
      index: Math.max(0, Math.floor(query.index ?? 0)),
    }), "utf8").toString("base64");
    const stdout = await this.ps(`
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$query = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String("${encoded}")) | ConvertFrom-Json
$wantedName = ([string]$query.name).ToLowerInvariant()
$wantedRole = ([string]$query.controlType).ToLowerInvariant()
$wantedWin = ([string]$query.windowTitle).ToLowerInvariant()
$idx = [int]$query.index

function Match-Element([System.Windows.Automation.AutomationElement]$el, [int]$indexRef) {
  try { $name = ([string]$el.Current.Name).ToLowerInvariant() } catch { $name = "" }
  try { $role = ([string]$el.Current.LocalizedControlType).ToLowerInvariant() } catch { $role = "" }
  $okName = ($wantedName -eq "" -or $name.Contains($wantedName))
  $okRole = ($wantedRole -eq "" -or $role.Contains($wantedRole))
  return $okName -and $okRole
}

$root = [System.Windows.Automation.AutomationElement]::RootElement
$startNodes = @()
if ($wantedWin -ne "") {
  $cond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::Window)
  $allWindows = $root.FindAll([System.Windows.Automation.TreeScope]::Children, $cond)
  foreach ($w in $allWindows) {
    if (([string]$w.Current.Name).ToLowerInvariant().Contains($wantedWin)) { $startNodes += ,$w }
  }
} else {
  $startNodes = ,$root
}

if ($startNodes.Count -eq 0) { throw "No window matching '$wantedWin' found." }

$matchCount = 0
$picked = $null
foreach ($start in $startNodes) {
  $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
  $stack = New-Object System.Collections.Stack
  $stack.Push($start)
  while ($stack.Count -gt 0) {
    $cur = $stack.Pop()
    if (Match-Element $cur $matchCount) {
      if ($matchCount -eq $idx) { $picked = $cur; break }
      $matchCount++
    }
    $child = $walker.GetFirstChild($cur)
    while ($child -ne $null) {
      $stack.Push($child)
      $child = $walker.GetNextSibling($child)
    }
  }
  if ($picked -ne $null) { break }
}
if ($picked -eq $null) { throw "No matching UIAutomation element found." }
$rect = $picked.Current.BoundingRectangle
[PSCustomObject]@{
  name = [string]$picked.Current.Name
  controlType = [string]$picked.Current.LocalizedControlType
  rect = @{
    x = [int]$rect.X
    y = [int]$rect.Y
    width = [int]$rect.Width
    height = [int]$rect.Height
  }
} | ConvertTo-Json -Compress
`, signal, 30000);
    return JSON.parse(stdout) as ElementInfo;
  }

  async getMarks(signal?: AbortSignal): Promise<Mark[]> {
    const { windows } = await this.listWindows(signal);
    return marksFromWindows(windows);
  }

  private async executable(signal?: AbortSignal): Promise<string> {
    this.executablePromise ??= this.resolveExecutable(signal);
    return await this.executablePromise;
  }

  private async resolveExecutable(signal?: AbortSignal): Promise<string> {
    const candidates = this.platform.isWsl
      ? ["powershell.exe", "/mnt/c/WINDOWS/System32/WindowsPowerShell/v1.0/powershell.exe", "pwsh.exe"]
      : ["powershell.exe", "powershell", "pwsh"];

    for (const candidate of candidates) {
      if (await commandExists(candidate, signal)) {
        await this.logger?.log("powershell.executable", {
          backend: this.name,
          executable: candidate,
          platform: this.platform.summary,
        });
        return candidate;
      }
    }

    const fallback = this.platform.isWsl ? "powershell.exe" : "powershell.exe";
    await this.logger?.log("powershell.executable", {
      backend: this.name,
      executable: fallback,
      platform: this.platform.summary,
      fallback: true,
    });
    return fallback;
  }

  private async ps(script: string, signal?: AbortSignal, timeoutMs = 15000): Promise<string> {
    const executable = await this.executable(signal);
    const encoded = Buffer.from(script, "utf16le").toString("base64");
    await this.logger?.log("powershell.start", {
      backend: this.name,
      executable,
      timeoutMs,
      scriptPreview: script.trim().slice(0, 1000),
    });
    const result = await execFile(executable, ["-NoProfile", "-NonInteractive", "-EncodedCommand", encoded], { timeoutMs, signal });
    await this.logger?.log("powershell.result", {
      backend: this.name,
      code: result.code,
      timedOut: result.timedOut,
      stdout: result.stdout,
      stderr: result.stderr,
    });
    if (result.code !== 0) {
      const stderrPreview = result.stderr.trim().slice(0, 400);
      throw new DesktopError(
        stderrPreview ? `PowerShell command failed: ${stderrPreview}` : "PowerShell command failed",
        { code: result.code, stderr: result.stderr, stdout: result.stdout, timedOut: result.timedOut },
      );
    }
    return result.stdout.trim();
  }

  async status(signal?: AbortSignal): Promise<Record<string, unknown>> {
    try {
      const executable = await this.executable(signal);
      await this.ps("$PSVersionTable.PSVersion.ToString()", signal, 5000);
      return { backend: this.name, platform: this.platform.summary, executable, ready: true };
    } catch (error) {
      return { backend: this.name, platform: this.platform.summary, ready: false, error: String(error) };
    }
  }

  async screenshot(options: ScreenshotOptions, signal?: AbortSignal): Promise<ScreenshotResult> {
    const region = options.region;
    const script = region ? regionScreenshotScript(region) : fullScreenshotScript();
    const stdout = await this.ps(script, signal, 30000);
    const { data, width, height, monitors } = extractScreenshotPayload(stdout, region);
    const path = await savePng(data, options.saveDir);
    return makeScreenshotResult(path, width, height, data, monitors);
  }

  async click(x: number, y: number, button: "left" | "right" | "middle", clicks: number, signal?: AbortSignal): Promise<Record<string, unknown>> {
    // Promoted to SendInput rather than the legacy mouse_event — produces
    // real INPUT events that survive DPI scaling and aren't filtered by
    // applications that distinguish synthetic vs. injected input.  Falls
    // back to mouse_event automatically only on PowerShell errors.
    const downFlag: Record<string, number> = { left: 0x0002, right: 0x0008, middle: 0x0020 };
    const upFlag: Record<string, number> = { left: 0x0004, right: 0x0010, middle: 0x0040 };
    const ABSOLUTE = 0x8000;
    const MOVE = 0x0001;
    await this.ps(`
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class PiInput {
  [StructLayout(LayoutKind.Sequential)] public struct MOUSEINPUT { public int dx; public int dy; public uint mouseData; public uint dwFlags; public uint time; public IntPtr dwExtraInfo; }
  [StructLayout(LayoutKind.Sequential)] public struct KEYBDINPUT { public ushort wVk; public ushort wScan; public uint dwFlags; public uint time; public IntPtr dwExtraInfo; }
  [StructLayout(LayoutKind.Sequential)] public struct HARDWAREINPUT { public uint uMsg; public ushort wParamL; public ushort wParamH; }
  [StructLayout(LayoutKind.Explicit)] public struct INPUT_UNION {
    [FieldOffset(0)] public MOUSEINPUT mi; [FieldOffset(0)] public KEYBDINPUT ki; [FieldOffset(0)] public HARDWAREINPUT hi;
  }
  [StructLayout(LayoutKind.Sequential)] public struct INPUT { public uint type; public INPUT_UNION u; }
  [DllImport("user32.dll")] public static extern uint SendInput(uint nInputs, [MarshalAs(UnmanagedType.LPArray)] INPUT[] pInputs, int cbSize);
  [DllImport("user32.dll")] public static extern int GetSystemMetrics(int nIndex);
}
"@
$sw = [PiInput]::GetSystemMetrics(0)
$sh = [PiInput]::GetSystemMetrics(1)
if ($sw -le 0) { $sw = 1 }
if ($sh -le 0) { $sh = 1 }
$ax = [int](([double]${x}) * 65535.0 / $sw)
$ay = [int](([double]${y}) * 65535.0 / $sh)
$move = New-Object PiInput+INPUT
$move.type = 0
$move.u.mi.dx = $ax
$move.u.mi.dy = $ay
$move.u.mi.dwFlags = ${MOVE} -bor ${ABSOLUTE}
[PiInput]::SendInput(1, @($move), [System.Runtime.InteropServices.Marshal]::SizeOf([type][PiInput+INPUT])) | Out-Null
for ($i = 0; $i -lt ${clicks}; $i++) {
  $down = New-Object PiInput+INPUT
  $down.type = 0
  $down.u.mi.dwFlags = ${downFlag[button]}
  $up = New-Object PiInput+INPUT
  $up.type = 0
  $up.u.mi.dwFlags = ${upFlag[button]}
  [PiInput]::SendInput(1, @($down), [System.Runtime.InteropServices.Marshal]::SizeOf([type][PiInput+INPUT])) | Out-Null
  [PiInput]::SendInput(1, @($up), [System.Runtime.InteropServices.Marshal]::SizeOf([type][PiInput+INPUT])) | Out-Null
}
`, signal);
    return { success: true, x, y, button, clicks, method: "sendinput" };
  }

  async typeText(text: string, signal?: AbortSignal): Promise<Record<string, unknown>> {
    const encoded = Buffer.from(text, "utf8").toString("base64");
    await this.ps(`
Add-Type -AssemblyName System.Windows.Forms
$text = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String("${encoded}"))
[System.Windows.Forms.Clipboard]::SetText($text)
[System.Windows.Forms.SendKeys]::SendWait("^v")
`, signal);
    return { success: true, length: text.length };
  }

  async hotkey(keys: string[], signal?: AbortSignal): Promise<Record<string, unknown>> {
    const sendKeys = toSendKeys(keys);
    await this.ps(`
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.SendKeys]::SendWait("${sendKeys.replace(/"/g, '`"')}")
`, signal);
    return { success: true, keys };
  }

  async scroll(x: number, y: number, clicks: number, direction: "up" | "down", signal?: AbortSignal): Promise<Record<string, unknown>> {
    const n = Math.max(1, clicks);
    const key = direction === "down" ? "{PGDN}" : "{PGUP}";
    const sk = key.repeat(n);
    // When coordinates are provided, use WindowFromPoint to focus the target window first.
    const focusBlock = x > 0 && y > 0 ? `
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class PiScrollFocus {
  [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X; public int Y; }
  [DllImport("user32.dll")] public static extern IntPtr WindowFromPoint(POINT p);
  [DllImport("user32.dll")] public static extern IntPtr GetAncestor(IntPtr h, uint f);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
}
"@ -Language CSharp
$pt = New-Object PiScrollFocus+POINT
$pt.X = ${x}; $pt.Y = ${y}
$child = [PiScrollFocus]::WindowFromPoint($pt)
$root = [PiScrollFocus]::GetAncestor($child, 2)
if ($root -ne [IntPtr]::Zero) { [PiScrollFocus]::SetForegroundWindow($root) | Out-Null }
Start-Sleep -Milliseconds 100` : "";
    await this.ps(`
$ErrorActionPreference = 'SilentlyContinue'
${focusBlock}
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.SendKeys]::SendWait("${sk}")
`, signal);
    return { success: true, x, y, clicks, direction, method: "keyboard" };
  }

  async getWindowText(maxChars = 50000, signal?: AbortSignal): Promise<{ text: string; length: number; truncated: boolean }> {
    const stdout = await this.ps(`
$ErrorActionPreference = 'SilentlyContinue'
Add-Type -AssemblyName System.Windows.Forms
$old = Get-Clipboard -Raw
[System.Windows.Forms.SendKeys]::SendWait("^a")
Start-Sleep -Milliseconds 300
[System.Windows.Forms.SendKeys]::SendWait("^c")
Start-Sleep -Milliseconds 500
$text = Get-Clipboard -Raw
try { if ($null -ne $old -and $old -ne '') { Set-Clipboard -Value $old } else { Set-Clipboard -Value '' } } catch {}
$maxLen = ${maxChars}
$truncated = $false
if ($null -eq $text) { $text = '' }
if ($text.Length -gt $maxLen) { $text = $text.Substring(0, $maxLen); $truncated = $true }
[PSCustomObject]@{ text = $text; length = $text.Length; truncated = $truncated } | ConvertTo-Json -Compress
`, signal, 15000);
    return JSON.parse(stdout) as { text: string; length: number; truncated: boolean };
  }

  async listWindows(signal?: AbortSignal): Promise<{ windows: WindowInfo[]; count: number }> {
    const stdout = await this.ps(`
$ErrorActionPreference = 'SilentlyContinue'
${windowType}
$active = [PiWindow]::GetForegroundWindow()
Get-Process | Where-Object { $_.MainWindowTitle -ne '' } | ForEach-Object {
  $r = New-Object PiWindow+RECT
  [PiWindow]::GetWindowRect($_.MainWindowHandle, [ref]$r) | Out-Null
  [PSCustomObject]@{
    id = $_.MainWindowHandle.ToString()
    title = $_.MainWindowTitle
    pid = [int]$_.Id
    app = $_.ProcessName
    x = [int]$r.Left
    y = [int]$r.Top
    width = [int]($r.Right - $r.Left)
    height = [int]($r.Bottom - $r.Top)
    active = ($_.MainWindowHandle -eq $active)
  }
} | ConvertTo-Json -Compress
`, signal);
    const windows = parseJsonArray<WindowInfo>(stdout);
    return { windows, count: windows.length };
  }

  async activeWindow(signal?: AbortSignal): Promise<{ window?: WindowInfo; found: boolean }> {
    const stdout = await this.ps(`
$ErrorActionPreference = 'Stop'
${windowType}
$h = [PiWindow]::GetForegroundWindow()
if ($h -eq [IntPtr]::Zero) {
  [PSCustomObject]@{ found = $false } | ConvertTo-Json -Compress
  exit
}
$r = New-Object PiWindow+RECT
[PiWindow]::GetWindowRect($h, [ref]$r) | Out-Null
$pidValue = 0
[PiWindow]::GetWindowThreadProcessId($h, [ref]$pidValue) | Out-Null
$sb = New-Object System.Text.StringBuilder 1024
[PiWindow]::GetWindowText($h, $sb, $sb.Capacity) | Out-Null
$proc = Get-Process -Id $pidValue -ErrorAction SilentlyContinue
[PSCustomObject]@{
  found = $true
  window = @{
    id = $h.ToString()
    title = $sb.ToString()
    pid = [int]$pidValue
    app = if ($proc) { $proc.ProcessName } else { $null }
    x = [int]$r.Left
    y = [int]$r.Top
    width = [int]($r.Right - $r.Left)
    height = [int]($r.Bottom - $r.Top)
    active = $true
  }
} | ConvertTo-Json -Compress
`, signal);
    const parsed = JSON.parse(stdout) as { found: boolean; window?: WindowInfo };
    return parsed;
  }

  async focusWindow(target: { id?: string; title?: string; pid?: number; app?: string }, signal?: AbortSignal): Promise<Record<string, unknown>> {
    const encodedTarget = Buffer.from(JSON.stringify(target), "utf8").toString("base64");
    const stdout = await this.ps(`
$ErrorActionPreference = 'Stop'
${windowType}
$target = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String("${encodedTarget}")) | ConvertFrom-Json
$candidates = @(Get-Process | Where-Object { $_.MainWindowTitle -ne '' })
if ($target.id) {
  $candidates = @($candidates | Where-Object { $_.MainWindowHandle.ToString() -eq [string]$target.id })
} elseif ($target.pid) {
  $candidates = @($candidates | Where-Object { $_.Id -eq [int]$target.pid })
} elseif ($target.title) {
  $needle = ([string]$target.title).ToLowerInvariant()
  $candidates = @($candidates | Where-Object { $_.MainWindowTitle.ToLowerInvariant().Contains($needle) })
} elseif ($target.app) {
  $needle = ([string]$target.app).ToLowerInvariant()
  $candidates = @($candidates | Where-Object { $_.ProcessName.ToLowerInvariant().Contains($needle) })
}
if ($candidates.Count -lt 1) {
  [PSCustomObject]@{ success = $false; reason = "no-match"; requested = $target } | ConvertTo-Json -Compress
  exit 0
}
$p = $candidates | Select-Object -First 1
$h = $p.MainWindowHandle
[PiWindow]::ShowWindowAsync($h, 9) | Out-Null
Start-Sleep -Milliseconds 100
$ok = [PiWindow]::SetForegroundWindow($h)
Start-Sleep -Milliseconds 150
$active = [PiWindow]::GetForegroundWindow()
[PSCustomObject]@{
  success = ($active -eq $h)
  requested = $target
  id = $h.ToString()
  title = $p.MainWindowTitle
  pid = [int]$p.Id
  app = $p.ProcessName
  active = ($active -eq $h)
} | ConvertTo-Json -Compress
`, signal);
    return JSON.parse(stdout) as Record<string, unknown>;
  }

  async launch(application: string, args: string[], signal?: AbortSignal): Promise<Record<string, unknown>> {
    const launchCandidates = this.platform.isWsl && !/\.(exe|bat|cmd|ps1)$/i.test(application)
      ? [application, `${application}.exe`]
      : [application];
    let lastError: unknown;

    for (const candidate of launchCandidates) {
      try {
        await this.launchCandidate(candidate, args, signal);
        return { success: true, application: candidate, requestedApplication: application, args };
      } catch (error) {
        lastError = error;
        await this.logger?.log("desktop_launch.candidate_failed", {
          requestedApplication: application,
          candidate,
          args,
          error: error instanceof Error ? error.message : String(error),
          details: typeof error === "object" && error !== null && "details" in error ? (error as { details?: unknown }).details : undefined,
        });
      }
    }

    throw lastError instanceof Error ? lastError : new DesktopError("Launch failed", { application, args, error: String(lastError) });
  }

  private async launchCandidate(application: string, args: string[], signal?: AbortSignal): Promise<void> {
    const encodedApp = Buffer.from(application, "utf8").toString("base64");
    const encodedArgs = Buffer.from(JSON.stringify(args), "utf8").toString("base64");
    await this.ps(`
$ErrorActionPreference = 'Stop'
$app = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String("${encodedApp}"))
$argsJson = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String("${encodedArgs}"))
$argList = @()
if ($argsJson -and $argsJson -ne '[]') {
  $parsed = ConvertFrom-Json $argsJson
  if ($parsed -is [array]) { $argList = @($parsed | ForEach-Object { [string]$_ }) }
  elseif ($null -ne $parsed) { $argList = @([string]$parsed) }
}
# Convert WSL-style paths (/mnt/c/Windows/System32/notepad.exe) to native
# Windows paths (C:\\Windows\\System32\\notepad.exe).  The model
# frequently obtains paths via 'where.exe' or 'which' under WSL, which
# returns the /mnt/<drive>/... form.  Windows PowerShell can't resolve
# those, so Get-Command and Start-Process both fail with "system cannot
# find the file specified".
if ($app -match '^/mnt/([a-zA-Z])(/.*)$') {
  $app = ($matches[1].ToUpper() + ':' + ($matches[2] -replace '/','\'))
}
# Resolve a bare name (e.g. "notepad") to its full executable path via PATH.
$resolved = $app
$cmd = Get-Command $app -ErrorAction SilentlyContinue -CommandType Application
if ($cmd) { $resolved = $cmd.Source }
# Attempt 1: direct Start-Process.  Defaults to ShellExecute on Windows
# when no -NoNewWindow / -RedirectStandard* flag is set, which is what
# we want for GUI launches.
$launched = $false
$directError = ''
try {
  if ($argList.Count -gt 0) { Start-Process -FilePath $resolved -ArgumentList $argList }
  else                       { Start-Process -FilePath $resolved }
  $launched = $true
} catch { $directError = $_.Exception.Message }
# Attempt 2: drop to .NET ProcessStartInfo with UseShellExecute=$true so
# Windows resolves the app via the App Paths registry / file
# associations / Start Menu entries (handles display names like
# "Notepad" without an .exe).  -UseShellExecute is NOT a valid
# Start-Process parameter, despite the misleading symmetry — it's a
# property of System.Diagnostics.ProcessStartInfo only.
if (-not $launched) {
  try {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $app
    $psi.UseShellExecute = $true
    if ($argList.Count -gt 0) { $psi.Arguments = ($argList -join ' ') }
    [void][System.Diagnostics.Process]::Start($psi)
    $launched = $true
  } catch {
    throw "Cannot launch '$app': direct=$directError; shell=$($_.Exception.Message)"
  }
}
`, signal);
  }

  async getCursorPos(signal?: AbortSignal): Promise<{ x: number; y: number }> {
    const stdout = await this.ps(`${mouseType}
$p = New-Object PiMouse+POINT
[PiMouse]::GetCursorPos([ref]$p) | Out-Null
[PSCustomObject]@{ x = $p.X; y = $p.Y } | ConvertTo-Json -Compress
`, signal);
    return JSON.parse(stdout) as { x: number; y: number };
  }

  async mouseMove(dx: number, dy: number, click: boolean, signal?: AbortSignal): Promise<Record<string, unknown>> {
    const pos = await this.getCursorPos(signal);
    const x = Math.max(1, pos.x + dx);
    const y = Math.max(1, pos.y + dy);
    await this.click(x, y, "left", click ? 1 : 0, signal);
    return { success: true, from: pos, to: { x, y }, clicked: click };
  }
}

function fullScreenshotScript(): string {
  return `
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$scr = [System.Windows.Forms.Screen]::AllScreens
$bounds = [System.Windows.Forms.SystemInformation]::VirtualScreen
$left = [int]$bounds.Left
$top = [int]$bounds.Top
$w = [int]$bounds.Width
$h = [int]$bounds.Height
if ($w -le 0 -or $h -le 0) {
  throw "Invalid virtual screen bounds: left=$left top=$top width=$w height=$h monitors=$($scr.Count)"
}
$bmp = New-Object System.Drawing.Bitmap -ArgumentList $w, $h
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($left, $top, 0, 0, $bmp.Size)
$g.Dispose()
$ms = New-Object System.IO.MemoryStream
$bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
$bmp.Dispose()
[PSCustomObject]@{
  monitors = [int]$scr.Count
  width = [int]$w
  height = [int]$h
  base64 = [Convert]::ToBase64String($ms.ToArray())
} | ConvertTo-Json -Compress
`;
}

function regionScreenshotScript(region: Rect): string {
  return `
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
$x = [int]${region.x}
$y = [int]${region.y}
$w = [int]${region.width}
$h = [int]${region.height}
if ($w -le 0 -or $h -le 0) {
  throw "Invalid screenshot region: x=$x y=$y width=$w height=$h"
}
$bmp = New-Object System.Drawing.Bitmap -ArgumentList $w, $h
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($x, $y, 0, 0, $bmp.Size)
$g.Dispose()
$ms = New-Object System.IO.MemoryStream
$bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
$bmp.Dispose()
[PSCustomObject]@{
  monitors = 1
  width = $w
  height = $h
  base64 = [Convert]::ToBase64String($ms.ToArray())
} | ConvertTo-Json -Compress
`;
}

function extractScreenshotPayload(stdout: string, region?: Rect): { data: string; width: number; height: number; monitors: number } {
  const json = extractJson(stdout);
  if (json) {
    const parsed = JSON.parse(json) as { base64?: string; width?: number; height?: number; monitors?: number };
    const data = validatePngBase64(parsed.base64 ?? "", stdout, json);
    return {
      data,
      width: Number(parsed.width ?? region?.width ?? 0),
      height: Number(parsed.height ?? region?.height ?? 0),
      monitors: Number(parsed.monitors ?? 1),
    };
  }

  const lines = stdout.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const meta = lines.find((line) => /^monitors=\d+\s+w=\d+\s+h=\d+$/.test(line));
  const data = lines
    .filter((line) => line !== meta)
    .join("")
    .replace(/\s+/g, "");
  const validData = validatePngBase64(data, stdout, meta);
  return {
    data: validData,
    width: Number(meta?.match(/w=(\d+)/)?.[1] ?? region?.width ?? 0),
    height: Number(meta?.match(/h=(\d+)/)?.[1] ?? region?.height ?? 0),
    monitors: Number(meta?.match(/monitors=(\d+)/)?.[1] ?? 1),
  };
}

function extractJson(stdout: string): string | undefined {
  const trimmed = stdout.trim();
  const start = trimmed.indexOf("{");
  const end = trimmed.lastIndexOf("}");
  if (start === -1 || end === -1 || end <= start) return undefined;
  return trimmed.slice(start, end + 1);
}

function validatePngBase64(data: string, stdout: string, meta?: string): string {
  if (!data) {
    throw new DesktopError("Screenshot command did not return PNG data.", {
      meta,
      stdoutPreview: stdout.slice(0, 500),
    });
  }
  if (!/^[A-Za-z0-9+/]+={0,2}$/.test(data)) {
    throw new DesktopError("Screenshot command returned invalid base64 data.", {
      meta,
      dataPreview: data.slice(0, 120),
    });
  }

  const bytes = Buffer.from(data, "base64");
  const isPng = bytes.length >= 8
    && bytes[0] === 0x89
    && bytes[1] === 0x50
    && bytes[2] === 0x4e
    && bytes[3] === 0x47
    && bytes[4] === 0x0d
    && bytes[5] === 0x0a
    && bytes[6] === 0x1a
    && bytes[7] === 0x0a;
  if (!isPng) {
    throw new DesktopError("Screenshot command returned base64 data, but it was not a PNG image.", {
      meta,
      dataPreview: data.slice(0, 120),
    });
  }

  return data;
}
