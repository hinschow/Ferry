# Build Ferry.exe -- a real Windows executable for the bridge console.
#
#   powershell -ExecutionPolicy Bypass -File build-windows-exe.ps1
#
# The exe is a LAUNCHER: it carries its own Python + Tk, but still runs the
# bridge_gui.py sitting next to it. That keeps the console's built-in
# self-update working -- a frozen copy of the code could never update itself.
#
# Needs PyInstaller (installed automatically if missing).

param(
    [string]$Python = "",
    [switch]$KeepBuildDir
)

# Native tools write to stderr all the time; judging by $LASTEXITCODE
# instead of letting PowerShell treat that as a terminating error.
$ErrorActionPreference = "Continue"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

if (-not (Test-Path "$here\bridge_gui.py")) {
    Write-Host "x bridge_gui.py not found next to this script" -ForegroundColor Red
    exit 1
}

# ---- find a python
if (-not $Python) {
    foreach ($cand in @("py -3", "python", "python3")) {
        $exe, $arg = $cand.Split(" ", 2)
        if (Get-Command $exe -ErrorAction SilentlyContinue) { $Python = $cand; break }
    }
}
if (-not $Python) { Write-Host "x no python found" -ForegroundColor Red; exit 1 }
Write-Host "[1/4] python: $Python"

function Invoke-Py { param([string[]]$PyArgs)
    $parts = @($Python.Split(" "))
    $head = @()
    if ($parts.Length -gt 1) { $head = $parts[1..($parts.Length - 1)] }
    & $parts[0] ($head + $PyArgs)
}

# ---- pyinstaller
Write-Host "[2/4] checking PyInstaller"
Invoke-Py @("-c", "import PyInstaller") 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "      installing..."
    Invoke-Py @("-m", "pip", "install", "--disable-pip-version-check", "pyinstaller")
    if ($LASTEXITCODE -ne 0) { Write-Host "x pip install pyinstaller failed" -ForegroundColor Red; exit 1 }
}

# ---- launcher stub
# The explicit imports are not decoration: PyInstaller scans THIS file to decide
# what to bundle, and it cannot see through runpy into bridge_gui.py.
$stub = @'
import os
import sys
import runpy

import base64, json, queue, re, shutil, socket, subprocess          # noqa: F401
import tempfile, threading, time, zlib, shlex, platform, urllib.request  # noqa: F401
import tkinter, tkinter.ttk, tkinter.filedialog                     # noqa: F401
import tkinter.messagebox, tkinter.simpledialog, tkinter.scrolledtext  # noqa: F401

HERE = os.path.dirname(os.path.abspath(sys.executable))
TARGET = os.path.join(HERE, "bridge_gui.py")

if not os.path.exists(TARGET):
    tkinter.Tk().withdraw()
    tkinter.messagebox.showerror("Ferry", "\u540c\u76ee\u5f55\u4e0b\u627e\u4e0d\u5230 bridge_gui.py\n\nFerry.exe \u5fc5\u987b\u548c bridge_gui.py \u653e\u5728\u540c\u4e00\u4e2a\u6587\u4ef6\u5939\u91cc\u3002\n\n\u5f53\u524d\u4f4d\u7f6e\uff1a\n" + HERE)
    sys.exit(1)

os.chdir(HERE)
# Reload passes bridge_gui.py back as an argument; drop it so the real
# script does not see a stray positional it never asked for.
sys.argv = [TARGET] + [a for a in sys.argv[1:] if os.path.abspath(a) != TARGET]
runpy.run_path(TARGET, run_name="__main__")
'@
Set-Content -Path "$here\_ferry_launcher.py" -Value $stub -Encoding UTF8

# ---- build
Write-Host "[3/4] building (takes a minute)"
$icon = Join-Path $here "assets\ferry.ico"
$pyArgs = @("-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed",
          "--name", "Ferry",
          "--distpath", "$here\_dist", "--workpath", "$here\_build",
          "--specpath", "$here\_build")
if (Test-Path $icon) { $pyArgs += @("--icon", $icon) }
$pyArgs += "$here\_ferry_launcher.py"
Invoke-Py $pyArgs
if ($LASTEXITCODE -ne 0) { Write-Host "x build failed" -ForegroundColor Red; exit 1 }

Move-Item -Force "$here\_dist\Ferry.exe" "$here\Ferry.exe"
if (-not $KeepBuildDir) {
    Remove-Item -Recurse -Force "$here\_dist", "$here\_build", "$here\_ferry_launcher.py" -ErrorAction SilentlyContinue
}

$mb = [math]::Round((Get-Item "$here\Ferry.exe").Length / 1MB, 1)
Write-Host "[4/4] done -- $here\Ferry.exe ($mb MB)" -ForegroundColor Green
Write-Host "      Double-click it. Right-click -> pin to taskbar / Start."
Write-Host "      It still runs bridge_gui.py next to it, so Reload keeps working."
