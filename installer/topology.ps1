# Installer-only topology contract. The product and deployers do not consume it.
# Local transport is reserved: path visibility/hostname equality do not prove
# namespace identity, and the authoritative deployers currently require SSH.
function Get-InstallerHttpUri {
    param([string]$Url)
    try { $uri = [Uri]$Url } catch { throw 'Use a valid HTTP(S) address without credentials, query or fragment.' }
    if (-not $uri.IsAbsoluteUri -or $uri.Scheme -notin @('http','https') -or $uri.UserInfo -or $uri.Query -or $uri.Fragment) {
        throw 'Use a valid HTTP(S) address without credentials, query or fragment.'
    }
    return $uri
}

function New-InstallerTopology {
    param([string]$HaUrl, [string]$HostName = '', [string]$User = '', [int]$Port = 22)
    $uri = Get-InstallerHttpUri $HaUrl
    if (-not $HostName) { $HostName = $uri.DnsSafeHost }
    if ($HostName -notmatch '^[a-zA-Z0-9_\[\]][a-zA-Z0-9_.:@\[\]-]*$' -or ($User -and $User -notmatch '^[a-zA-Z0-9_][a-zA-Z0-9_.-]*$')) {
        throw 'Invalid SSH filesystem host or username.'
    }
    if ($Port -lt 1 -or $Port -gt 65535) { throw 'SSH port must be between 1 and 65535.' }
    return [ordered]@{
        ha_api_url = $HaUrl.TrimEnd('/')
        filesystem_transport = 'ssh'
        filesystem_host = $HostName
        filesystem_user = $User
        filesystem_port = $Port
        appdaemon_config_root = ''
        appdaemon_http_url = ''
        appdaemon_restart_strategy = 'manual'
        capabilities = [ordered]@{
            ssh_filesystem_available = $false
            appdaemon_root_detected = $false
            appdaemon_http_reachable = $false
            supervisor_restart_available = $false
            ha_www_access_available = $false
            local_filesystem_supported = $false
        }
    }
}

function Test-InstallerSsh {
    param($Topology)
    $destination = if ($Topology.filesystem_user) { "$($Topology.filesystem_user)@$($Topology.filesystem_host)" } else { $Topology.filesystem_host }
    $null = & ssh -p $Topology.filesystem_port -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new $destination 'echo ok' 2>$null
    return ($LASTEXITCODE -eq 0)
}

function Test-InstallerSupervisor {
    param([string]$HaUrl, [string]$Token)
    try {
        $services = Invoke-RestMethod "$HaUrl/api/services" -Headers @{Authorization="Bearer $Token"} -TimeoutSec 10
        return [bool]($services | Where-Object { $_.domain -eq 'hassio' -and $_.services.PSObject.Properties.Name -contains 'addon_restart' })
    } catch { return $false }
}

function Test-InstallerHaWww {
    param($Topology, [string]$HaVersion, [bool]$ConfirmedSplitFilesystem = $false)
    # A writable /config on a different host can belong to an unrelated HA.
    # Split targets require an explicit operator binding as well as HA markers.
    $hostOnly = $Topology.filesystem_host -replace '^.*@',''
    if ($hostOnly -ne ([Uri]$Topology.ha_api_url).DnsSafeHost -and -not $ConfirmedSplitFilesystem) { return $false }
    try {
        $markers = Invoke-WizardSsh -Command "sudo test -f '/config/configuration.yaml' && sudo test -f '/config/.HA_VERSION' && sudo test -w '/config' && sudo cat '/config/.HA_VERSION'"
        if ($markers.Trim() -ne $HaVersion) { return $false }
        $null = Invoke-WizardSsh -Command "if sudo test -e '/config/www'; then sudo test -w '/config/www'; fi"
        return $true
    } catch { return $false }
}
