<#
.SYNOPSIS
Undoes register-host.ps1: removes the host's registry keys and its dev-host folder.

.DESCRIPTION
Removes HKCU\Software\Google\Chrome\NativeMessagingHosts\com.redactit.host and the Edge
equivalent where present, and %LOCALAPPDATA%\Redactit\dev-host (only the launcher and the
manifest this checkout wrote; the rest of %LOCALAPPDATA%\Redactit, such as the vault and
models, is left alone). Run with -WhatIf to see the steps without making them.
#>
[CmdletBinding(SupportsShouldProcess)]
param()

$ErrorActionPreference = "Stop"
$HostName = "com.redactit.host"
$Folder = Join-Path $env:LOCALAPPDATA "Redactit\dev-host"
$Keys = @("HKCU:\Software\Google\Chrome\NativeMessagingHosts\$HostName",
          "HKCU:\Software\Microsoft\Edge\NativeMessagingHosts\$HostName")

foreach ($Key in $Keys) {
    if ((Test-Path $Key) -and $PSCmdlet.ShouldProcess($Key, "Remove the host registration")) {
        Remove-Item -Path $Key -Force
    }
}
foreach ($Name in @("redactit-host.bat", "$HostName.json")) {
    $File = Join-Path $Folder $Name
    if ((Test-Path $File) -and $PSCmdlet.ShouldProcess($File, "Remove")) {
        Remove-Item -Path $File -Force
    }
}
if ((Test-Path $Folder) -and -not (Get-ChildItem $Folder) -and $PSCmdlet.ShouldProcess($Folder, "Remove the empty folder")) {
    Remove-Item -Path $Folder -Force
}
if (-not $WhatIfPreference) { Write-Host "Host '$HostName' unregistered." }
