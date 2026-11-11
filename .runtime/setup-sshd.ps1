$ErrorActionPreference = 'Stop'
$resultPath = 'G:\pepper\.runtime\ssh-install-result.txt'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
$isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
@(
    'STARTED'
    "User=$($identity.Name)"
    "Administrator=$isAdmin"
) | Set-Content -LiteralPath $resultPath -Encoding UTF8

if (-not $isAdmin) {
    throw 'The installer process is not elevated.'
}

try {
    $capability = Get-WindowsCapability -Online -Name 'OpenSSH.Server*'
    if ($capability.State -ne 'Installed') {
        Add-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0' | Out-Null
    }

    Set-Service -Name sshd -StartupType Automatic
    Start-Service -Name sshd

    $rule = Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue
    if ($null -eq $rule) {
        New-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' `
            -DisplayName 'OpenSSH Server (sshd)' `
            -Enabled True -Direction Inbound -Protocol TCP `
            -Action Allow -LocalPort 22 | Out-Null
    } else {
        Enable-NetFirewallRule -Name 'OpenSSH-Server-In-TCP'
    }

    $service = Get-Service -Name sshd
    $listener = Get-NetTCPConnection -State Listen -LocalPort 22 -ErrorAction SilentlyContinue
    @(
        'SUCCESS'
        "Capability=$((Get-WindowsCapability -Online -Name 'OpenSSH.Server*').State)"
        "Service=$($service.Status)"
        "StartType=$($service.StartType)"
        "Listening=$([bool]$listener)"
    ) | Set-Content -LiteralPath $resultPath -Encoding UTF8
} catch {
    @(
        'FAILED'
        $_.Exception.ToString()
    ) | Set-Content -LiteralPath $resultPath -Encoding UTF8
    exit 1
}
