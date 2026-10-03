<#
.SYNOPSIS
Registers this checkout's native host for the unpacked extension, for development checks.

.DESCRIPTION
Until the Phase 7 installers exist, this is how a developer lets Chrome (or Edge) start the
host from this repository. It writes, under %LOCALAPPDATA%\Redactit\dev-host:
  - redactit-host.bat: starts the base Python isolated (-I -S) with only this checkout's
    virtual environment on the path, as PLAN section 7 describes the real launcher;
  - com.redactit.host.json: the host manifest, allowing only the unpacked extension's ID;
and points HKCU\Software\<browser>\NativeMessagingHosts\com.redactit.host at the manifest.
Nothing outside the current user's registry and that folder changes.
unregister-host.ps1 removes both. Run with -WhatIf to see the steps without making them.

.PARAMETER Browser
chrome (default), edge or both.
#>
[CmdletBinding(SupportsShouldProcess)]
param([ValidateSet("chrome", "edge", "both")][string]$Browser = "chrome")

$ErrorActionPreference = "Stop"
$ExtensionId = "ejcaindhhnocdeolkgmcfnbemobjhllk"  # fixed by the development key in extension/manifest.json
$HostName = "com.redactit.host"
$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$VenvPython = Join-Path $Repo ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    throw "No virtual environment at $VenvPython. Run 'py -3.12 -m uv sync' in $Repo first."
}
# The venv's python.exe is a redirector; the launcher runs the interpreter it points to.
$BasePython = (& $VenvPython -c "import sys; print(sys._base_executable)").Trim()
$SitePackages = (& $VenvPython -c "import sysconfig; print(sysconfig.get_paths()['purelib'])").Trim()

$Folder = Join-Path $env:LOCALAPPDATA "Redactit\dev-host"
$Launcher = Join-Path $Folder "redactit-host.bat"
$Manifest = Join-Path $Folder "$HostName.json"
$Code = "import site; site.addsitedir(r'$SitePackages'); from redactit.hosts.native import main; main()"

$LauncherText = @(
    "@echo off",
    "`"$BasePython`" -I -S -c `"$Code`" --manifest `"$Manifest`" %*"
) -join "`r`n"
$ManifestText = [ordered]@{
    name            = $HostName
    description     = "Redactit: redacts sensitive data on this computer before it reaches an AI chat site"
    path            = $Launcher
    type            = "stdio"
    allowed_origins = @("chrome-extension://$ExtensionId/")
} | ConvertTo-Json

$Keys = switch ($Browser) {
    "chrome" { @("HKCU:\Software\Google\Chrome\NativeMessagingHosts\$HostName") }
    "edge"   { @("HKCU:\Software\Microsoft\Edge\NativeMessagingHosts\$HostName") }
    "both"   { @("HKCU:\Software\Google\Chrome\NativeMessagingHosts\$HostName",
                 "HKCU:\Software\Microsoft\Edge\NativeMessagingHosts\$HostName") }
}

if ($PSCmdlet.ShouldProcess($Folder, "Write the launcher and host manifest")) {
    New-Item -ItemType Directory -Force -Path $Folder | Out-Null
    [IO.File]::WriteAllText($Launcher, $LauncherText + "`r`n", [Text.Encoding]::ASCII)
    [IO.File]::WriteAllText($Manifest, $ManifestText, (New-Object Text.UTF8Encoding $false))
}
foreach ($Key in $Keys) {
    if ($PSCmdlet.ShouldProcess($Key, "Point the browser at $Manifest")) {
        New-Item -Force -Path $Key | Out-Null
        Set-Item -Path $Key -Value $Manifest
    }
}
if (-not $WhatIfPreference) {
    Write-Host "Host '$HostName' registered for $Browser; extension ID $ExtensionId."
    Write-Host "Launcher: $Launcher"
}
