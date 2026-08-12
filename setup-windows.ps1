# ============================================================================
#  Bridge Console - Windows side one-shot setup
#
#  Run in an ADMINISTRATOR PowerShell:
#      powershell -ExecutionPolicy Bypass -File setup-windows.ps1 -PubKey "ssh-ed25519 AAAA... comment"
#
#  Optional:
#      -Alias      SSH host alias to create (default: derived from -ServerHost)
#      -ServerHost Server IP or hostname (adds a Host block to ~/.ssh/config)
#      -Identity   Private key path used to reach that server
#      -Port       Reverse tunnel port on the server side (default 2222)
#      -LoopbackOnly  Restrict sshd to 127.0.0.1 only (recommended)
#      -AutoStart     Create a Startup shortcut for the console
#
#  Everything is append-only and backed up. Config is validated before restart,
#  and rolled back automatically if invalid.
# ============================================================================
param(
    [string]$PubKey = "",
    [string]$Alias = "",
    [string]$ServerHost = "",
    [string]$Identity = "",
    [int]$Port = 2222,
    [switch]$LoopbackOnly,
    [switch]$AutoStart
)

$ErrorActionPreference = "Stop"
function Say($m) { Write-Output $m }

# Common mistake: passing the public key as -Identity
if ($Identity -and $Identity.EndsWith(".pub")) {
    Write-Output "[!] -Identity should be the PRIVATE key path, you gave a public key: $Identity"
    Write-Output ("    Drop the trailing .pub, e.g. " + $Identity.Substring(0, $Identity.Length - 4))
    exit 1
}
function Warn($m) { Write-Output ("  ! " + $m) }

# ---- must be admin
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Say "This script must run in an ADMINISTRATOR PowerShell."
    exit 1
}

Say "[1/7] Installing OpenSSH Server"
$cap = Get-WindowsCapability -Online -Name OpenSSH.Server* | Select-Object -First 1
if ($cap.State -ne "Installed") {
    Add-WindowsCapability -Online -Name $cap.Name | Out-Null
    Say "      installed"
} else { Say "      already installed" }
Set-Service -Name sshd -StartupType Automatic
if ((Get-Service sshd).Status -ne "Running") { Start-Service sshd }
Say ("      service: " + (Get-Service sshd).Status)

Say "[2/7] Authorizing server public key"
if ($PubKey -and $PubKey.Trim()) {
    $grp = (Get-LocalGroup -SID 'S-1-5-32-544').Name
    $isAdmin = [bool](Get-LocalGroupMember -Group $grp -EA SilentlyContinue |
                      Where-Object { $_.Name -like "*\$env:USERNAME" })
    if ($isAdmin) {
        $ak = "C:\ProgramData\ssh\administrators_authorized_keys"
    } else {
        New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\.ssh" | Out-Null
        $ak = "$env:USERPROFILE\.ssh\authorized_keys"
    }
    $tag = ($PubKey.Trim() -split '\s+')[-1]
    if ((Test-Path $ak) -and (Select-String -Path $ak -SimpleMatch $tag -Quiet)) {
        Say "      key already present, skipped"
    } else {
        Add-Content $ak $PubKey.Trim() -Encoding ASCII
        Say ("      appended to " + $ak)
    }
    if ($isAdmin) {
        icacls $ak /inheritance:r /grant "${grp}:F" /grant "SYSTEM:F" | Out-Null
    }
} else { Warn "no -PubKey given, skipping" }

Say "[3/7] Hardening sshd (loopback only)"
if ($LoopbackOnly) {
    $c = "C:\ProgramData\ssh\sshd_config"
    Copy-Item $c "$c.bak" -Force
    $lines = @(Get-Content $c) | Where-Object { $_ -notmatch "^\s*ListenAddress\s+127\.0\.0\.1\s*$" }
    # ListenAddress MUST go before any Match block - it is illegal inside one
    $idx = -1
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match "^\s*Match\s+") { $idx = $i; break }
    }
    if ($idx -lt 1) { $out = @($lines) + @("ListenAddress 127.0.0.1") }
    else { $out = @($lines[0..($idx-1)]) + @("ListenAddress 127.0.0.1","") + @($lines[$idx..($lines.Count-1)]) }
    Set-Content -Path $c -Value $out -Encoding ASCII
    $sshdExe = Join-Path $env:SystemRoot "System32\OpenSSH\sshd.exe"
    & $sshdExe -t 2>&1 | ForEach-Object { Say ("      " + $_) }
    if ($LASTEXITCODE -eq 0) {
        Restart-Service sshd
        Say "      OK - sshd now listens on 127.0.0.1 only"
    } else {
        Copy-Item "$c.bak" $c -Force
        Restart-Service sshd
        Warn "config invalid, rolled back"
    }
} else { Say "      skipped (pass -LoopbackOnly to enable)" }

Say "[4/7] Adding SSH host alias"
if ($ServerHost) {
    if (-not $Alias) { $Alias = ($ServerHost -replace '[^A-Za-z0-9]', '') }
    $cfg = "$env:USERPROFILE\.ssh\config"
    New-Item -ItemType Directory -Force -Path (Split-Path $cfg) | Out-Null
    if (-not (Test-Path $cfg)) { New-Item -ItemType File -Path $cfg | Out-Null }
    Copy-Item $cfg "$cfg.bak" -Force
    if (Select-String -Path $cfg -Pattern "^\s*Host\s+$Alias\s*$" -Quiet) {
        Say "      alias '$Alias' already exists, skipped"
    } else {
        Add-Content $cfg ""
        Add-Content $cfg "Host $Alias"
        Add-Content $cfg "    HostName $ServerHost"
        Add-Content $cfg "    User root"
        if ($Identity) { Add-Content $cfg "    IdentityFile $Identity" }
        # 127.0.0.1 NOT localhost - Windows resolves localhost to ::1 first
        Add-Content $cfg "    RemoteForward $Port 127.0.0.1:22"
        Add-Content $cfg "    ServerAliveInterval 30"
        Add-Content $cfg "    ServerAliveCountMax 3"
        Add-Content $cfg "    StrictHostKeyChecking accept-new"
        Say "      added alias '$Alias' -> $ServerHost (RemoteForward $Port)"
    }
} else { Say "      skipped (pass -ServerHost to add)" }

Say "[5/7] Desktop shortcut"
# A shortcut with the real icon is the no-build way to get a proper-looking app.
# Want an actual Ferry.exe instead? Run build-windows-exe.ps1.
$gui = Join-Path $PSScriptRoot "bridge_gui.py"
$pyw = (Get-Command pythonw -EA SilentlyContinue).Source
$ico = Join-Path $PSScriptRoot "assets\ferry.ico"
if (-not (Test-Path $gui)) { Warn "bridge_gui.py not found next to this script" }
elseif (-not $pyw) { Warn "pythonw not found on PATH" }
else {
    $sh = New-Object -ComObject WScript.Shell
    $lnk = Join-Path $PSScriptRoot "Ferry.lnk"
    $s = $sh.CreateShortcut($lnk)
    $s.TargetPath = $pyw
    $s.Arguments = "`"$gui`""
    $s.WorkingDirectory = $PSScriptRoot
    $s.Description = "Ferry bridge console"
    if (Test-Path $ico) { $s.IconLocation = $ico }
    $s.Save()
    Say ("      created " + $lnk + "   (right-click -> pin to taskbar)")
}

Say "[6/7] Startup shortcut"
if ($AutoStart) {
    if (-not (Test-Path $gui)) { Warn "bridge_gui.py not found next to this script" }
    elseif (-not $pyw) { Warn "pythonw not found on PATH" }
    else {
        $lnk = Join-Path ([Environment]::GetFolderPath("Startup")) "BridgeConsole.lnk"
        $sh = New-Object -ComObject WScript.Shell
        $s = $sh.CreateShortcut($lnk)
        $s.TargetPath = $pyw
        $s.Arguments = "`"$gui`" --auto-tunnel --minimized"
        $s.WorkingDirectory = $PSScriptRoot
        if (Test-Path $ico) { $s.IconLocation = $ico }
        $s.WindowStyle = 7
        $s.Save()
        Say ("      created " + $lnk)
    }
} else { Say "      skipped (pass -AutoStart to enable)" }

Say "[7/7] Summary"
Say ("      sshd        : " + (Get-Service sshd).Status)
Get-NetTCPConnection -LocalPort 22 -State Listen -EA SilentlyContinue |
    ForEach-Object { Say ("      listening   : " + $_.LocalAddress + ":" + $_.LocalPort) }
if ($Alias) {
    Say ("      test with   : ssh -N " + $Alias)
    Say  "      then on the server: bridge-check  /  bridge-mount 'D:\your\path'"
}
Say ""
Say "Done."
