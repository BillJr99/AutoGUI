# AutoGUI dependency installer — Windows.
#
# Idempotent: every dep is checked before install and skipped when present.
# Loud by design: every command is echoed before running.
#
# Run manually (PowerShell):
#     powershell -ExecutionPolicy Bypass -File scripts\install-dependencies.ps1
# or via the bundled cmd shim:
#     scripts\install-dependencies.cmd
#
# Run automatically: set "install_dependencies": true in config.json
# (mainline) or pi-extension\config.json — AutoGUI invokes this once at
# startup before initialising the agent.
#
# Sections:
#   1. winget availability + system installs (Tesseract, ImageMagick)
#   2. Python deps (pip)
#   3. Playwright Chromium
#   4. Pi-extension Node deps

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path "$PSScriptRoot\..").Path
Set-Location $ProjectRoot

function Run([string[]]$cmd) {
  Write-Host "[install] `$ $($cmd -join ' ')"
  $exe = $cmd[0]
  $rest = if ($cmd.Length -gt 1) { $cmd[1..($cmd.Length - 1)] } else { @() }
  & $exe @rest
  if ($LASTEXITCODE -ne 0) {
    Write-Host "[install] command failed with code $LASTEXITCODE"
    throw "Command failed: $($cmd -join ' ')"
  }
}

function Have-Cmd([string]$name) {
  return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

function Have-PyModule([string]$module, [string]$py = "python") {
  & $py -c "import $module" 2>$null
  return ($LASTEXITCODE -eq 0)
}

# --- 1. System installs via winget ------------------------------------------

if (-not (Have-Cmd "winget")) {
  Write-Host "[install] winget not found. Install App Installer from the Microsoft Store, then re-run."
  exit 1
}

$wingetCommon = @(
  "--silent",
  "--accept-package-agreements",
  "--accept-source-agreements"
)

# Tesseract — for desktop_click_text / desktop_find_text.
if (-not (Have-Cmd "tesseract")) {
  Write-Host "[need ] tesseract missing"
  Run @("winget", "install", "--id=UB-Mannheim.TesseractOCR") + $wingetCommon
} else {
  Write-Host "[skip ] tesseract already on PATH"
}

# ImageMagick — Set-of-Mark overlay + failure GIF assembly.
if (-not (Have-Cmd "magick") -and -not (Have-Cmd "convert")) {
  Write-Host "[need ] ImageMagick missing"
  Run @("winget", "install", "--id=ImageMagick.ImageMagick") + $wingetCommon
} else {
  Write-Host "[skip ] ImageMagick already installed"
}

# --- 2. Python deps (mainline) ----------------------------------------------

$Python = if ($Env:PYTHON) { $Env:PYTHON } else { "python" }

if (Test-Path "requirements.txt") {
  if (-not (Have-Cmd $Python)) {
    Write-Host "[install] $Python not found on PATH; install Python 3 first."
  } else {
    & $Python -m pip --version 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
      Write-Host "[install] bootstrapping pip"
      Run @($Python, "-m", "ensurepip", "--upgrade")
    }

    Write-Host "[install] pip install -r requirements.txt"
    Run @($Python, "-m", "pip", "install", "--quiet", "-r", "requirements.txt")

    # Optional pip packages aligned with the click ladder + browser tools.
    foreach ($pair in @(
      @("pyperclip",     "pyperclip"),
      @("pytesseract",   "pytesseract"),
      @("playwright",    "playwright"),
      @("uiautomation",  "uiautomation"),
      @("win32gui",      "pywin32")
    )) {
      $module, $package = $pair
      if (Have-PyModule $module $Python) {
        Write-Host "[skip ] python module $module already importable"
      } else {
        Write-Host "[need ] python module $module missing"
        Run @($Python, "-m", "pip", "install", "--quiet", $package)
      }
    }

    # Playwright Chromium.
    if (Have-PyModule "playwright" $Python) {
      Run @($Python, "-m", "playwright", "install", "chromium")
    }
  }
} else {
  Write-Host "[skip ] no requirements.txt at $ProjectRoot — mainline Python deps not installed"
}

# --- 3. Pi-extension Node deps ---------------------------------------------

if ((Test-Path "pi-extension\package.json") -and (Have-Cmd "npm")) {
  if (-not (Test-Path "pi-extension\node_modules")) {
    Write-Host "[install] cd pi-extension; npm install"
    Push-Location pi-extension
    try { Run @("npm", "install", "--silent") } finally { Pop-Location }
  } else {
    Write-Host "[skip ] pi-extension\node_modules already present"
  }
  Push-Location pi-extension
  try { Run @("npx", "--yes", "playwright", "install", "chromium") } catch { Write-Host "[install] npx playwright install chromium failed (non-fatal)" } finally { Pop-Location }
} elseif (Test-Path "pi-extension\package.json") {
  Write-Host "[note ] npm not on PATH; skipping pi-extension node deps"
}

Write-Host "[install] done."
