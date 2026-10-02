<#
.SYNOPSIS
  Scene Studio guided installer — the release/repo root entry point.

.DESCRIPTION
  This is the ordinary user's entry point. It asks a short sequence of plain
  questions, discovers everything that can be discovered read-only, shows a
  review, and then drives the EXISTING validated installer and deployers —
  it does not duplicate their safety logic.

  Three INDEPENDENT endpoints are modeled; none is assumed to imply another:

    1. Home Assistant API   (https://homeassistant.example:8123)
    2. SSH filesystem target (HA host for the common add-on setup)
    3. Scene Studio HTTP    (http://appdaemon-host:5050) — the AppDaemon
       add-on serving the Workbench and the Scene Studio API.

  The Scene Studio HTTP endpoint is normally INFERRED from the ssh target
  plus the default AppDaemon port (5050), tested read-only, and only asked
  for when inference does not work. Home Assistant Core and AppDaemon do
  NOT have to share a machine or hostname.

  What the wizard changes remotely, in order:
    1. nothing — dependency/connection/provider checks are read-only;
    2. a REVIEW of the exact apps.yaml/secrets.yaml changes, requiring an
       explicit confirmation;
    3. a bounded, marker-delimited scene_studio block in apps.yaml and
       (only for enabled providers) secret entries in secrets.yaml — the
       originals are backed up first and restored automatically if the
       install fails;
    4. the backend + Workbench deployment through the validated installer
       (backup/stage/hash/verify/rollback gates intact), which restarts
       AppDaemon and verifies the Scene Studio API;
    5. OPTIONALLY the prebuilt Scene Studio Home Assistant card deployed to
       a Scene Studio-owned /config/www/scene-studio-card/ location. The
       card never modifies any dashboard; registration guidance is printed.

  If an unexpected failure occurs, the wizard prints the failed stage and a
  path to a SANITIZED support report ZIP (versions, endpoints, probes, and
  a redacted log — never tokens, keys, or secret values) that can be sent
  to the maintainer for diagnosis.

  Advanced usage (power users / CI):
    - -ProfilePath <file>  run the existing installer with a hand-written
      profile (no questions asked);
    - -Unattended -Answers <file> [-Yes]  run the guided flow from a JSON
      answers file (same questions, no prompts). Secrets come from
      environment variables (SCENE_STUDIO_HA_TOKEN, SCENE_STUDIO_HUE_APP_KEY)
      and are NEVER read from or written to the answers file.

  Answers JSON keys (all optional unless noted):
      ha_url (required), ssh_host (default = HA hostname), ssh_user, ssh_port,
      appdaemon_config_root (null = auto-detect), addon_root_choice (1-based,
      when several candidates are found), store_root,
      appdaemon_http_url (null = infer from the ssh target + port 5050),
      install_ha_card (bool, default true when HA filesystem capability is confirmed),
      ha_config_filesystem_confirmed (bool, explicit binding for split/alias hosts),
      providers: { ha_light: bool, hue: {enabled, host, bridge_id},
                   wled: {enabled, host}, hyperhdr: {enabled, host,
                   default_policy} },
      confirm_install: bool (equivalent of -Yes)
#>

[CmdletBinding()]
param(
    [string]$ProfilePath = '',
    [switch]$Unattended,
    [string]$Answers = '',
    [switch]$Yes,
    [int]$SshPort = 0,
    [string]$AppDaemonHttpUrl = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ($PSVersionTable.PSVersion.Major -lt 7) {
    throw 'PowerShell 7 (pwsh) is required. Install it from https://aka.ms/powershell and re-run: pwsh -File Install-SceneStudio.ps1'
}

# ---------------------------------------------------------------------------
# layout: two supported positions —
#   release tree : the wizard is the ROOT entry point (Install-SceneStudio.ps1
#                  next to installer/, backend/, workbench/...)
#   dev checkout : the wizard lives at installer/Install-SceneStudio.ps1
# In both cases $BundleRoot ends up as the product root.
# ---------------------------------------------------------------------------

if (Test-Path -LiteralPath (Join-Path (Join-Path $PSScriptRoot 'installer') 'deploy_scene_studio.ps1')) {
    $BundleRoot = (Resolve-Path $PSScriptRoot).Path
    $InternalInstaller = Join-Path (Join-Path $BundleRoot 'installer') 'deploy_scene_studio.ps1'
    $ProfileTool = Join-Path (Join-Path $BundleRoot 'installer') 'scene-studio-profile.py'
} elseif (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'deploy_scene_studio.ps1')) {
    $BundleRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
    $InternalInstaller = Join-Path $PSScriptRoot 'deploy_scene_studio.ps1'
    $ProfileTool = Join-Path $PSScriptRoot 'scene-studio-profile.py'
} else {
    throw 'Install-SceneStudio.ps1 could not locate the installer support tree. Re-extract the full release.'
}
$CardDist = Join-Path (Join-Path (Join-Path (Join-Path $BundleRoot 'home-assistant') 'scene-studio-card') 'dist') 'scene-studio-card.js'
$CardWwwDir = '/config/www/scene-studio-card'
$CardWwwFile = "$CardWwwDir/scene-studio-card.js"
foreach ($required in @($InternalInstaller, $ProfileTool)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Install-SceneStudio.ps1 could not locate a required file ($required). Re-extract the full release."
    }
}
$Version = 'dev'
$VersionFile = Join-Path $BundleRoot 'VERSION'
if (Test-Path -LiteralPath $VersionFile) {
    $Version = (Get-Content -LiteralPath $VersionFile -Raw).Trim()
}

# ---------------------------------------------------------------------------
# diagnostics: stage tracking, log capture, probes, support report
# ---------------------------------------------------------------------------

$script:Stage = 'startup'
$script:RemoteChangeState = 'none'
$script:Log = [System.Collections.Generic.List[string]]::new()
$script:Probes = [System.Collections.Generic.List[string]]::new()
$script:HaVersion = ''
$script:InstallKind = 'fresh'
$script:EnabledProviderTypes = @()
$script:ProviderReachability = @()

function Add-WizardLog {
    param([string]$Line)
    $script:Log.Add("$(Get-Date -Format 'yyyy-MM-ddTHH:mm:ss') $Line")
}

function Record-Probe {
    param([string]$Name, [string]$Result)
    $script:Probes.Add("$Name=$Result")
}

function Write-Step {
    param([string]$Text)
    Write-Host ''
    Write-Host "==> $Text" -ForegroundColor Cyan
    Add-WizardLog "STEP $Text"
}

function Write-Detail {
    param([string]$Text)
    Write-Host "    $Text"
    Add-WizardLog "  $Text"
}

function Write-WizardWarning {
    param([string]$Text)
    Write-Warning $Text
    Add-WizardLog "  WARNING $Text"
}

function Get-SshClientVersion {
    try {
        return (((& ssh -V) 2>&1 | Out-String).Trim())
    } catch {
        return 'unavailable'
    }
}

function Write-SupportReport {
    # Build the sanitized support archive and tell the user where it is.
    # NEVER include: the HA bearer token, the Hue application key, any
    # secrets.yaml value, auth headers, raw device payloads, or arbitrary
    # HA state dumps. The log is defensively scrubbed of the secret values
    # this process held, even though the wizard never prints them.
    param(
        [Parameter(Mandatory = $true)][string]$Stage,
        [Parameter(Mandatory = $true)][string]$Reason,
        [Parameter(Mandatory = $true)][string]$RemoteChangeState,
        [Parameter(Mandatory = $true)][hashtable]$Context
    )
    try {
        $stamp = "$(Get-Date -Format 'yyyyMMdd-HHmmss')-$([Guid]::NewGuid().ToString('N'))"
        $reportDir = Join-Path ([IO.Path]::GetTempPath()) "scene-studio-support-$stamp"
        New-Item -ItemType Directory -Path $reportDir | Out-Null

        Add-WizardLog "FAILURE stage=$Stage reason=$Reason remote_changes=$RemoteChangeState"

        $secretValues = @()
        foreach ($candidate in @($Context['HaToken'], $Context['HueAppKey'], $env:SCENE_STUDIO_GITHUB_TOKEN)) {
            if ($candidate) { $secretValues += [string]$candidate }
        }
        $scrub = {
            param([string]$Text)
            foreach ($secret in $secretValues) {
                if ($secret) {
                    $Text = $Text.Replace($secret, '[redacted]')
                }
            }
            return $Text
        }

        $report = [ordered]@{
            scene_studio_version = $Version
            installer_stage      = $Stage
            error                = & $scrub $Reason
            remote_changes       = $RemoteChangeState
            install_kind         = $script:InstallKind
            generated_at         = (Get-Date).ToUniversalTime().ToString('o')
            environment          = [ordered]@{
                os               = [System.Runtime.InteropServices.RuntimeInformation]::OSDescription
                powershell       = $PSVersionTable.PSVersion.ToString()
                python           = $Context['PythonVersion']
                ssh_client       = $Context['SshVersion']
            }
            endpoints            = [ordered]@{
                ha_api           = $Context['HaApiUrl']
                ssh              = $Context['SshTarget']
                appdaemon_http   = $Context['AppDaemonHttp']
                appdaemon_root   = $Context['AddonRoot']
            }
            topology             = $script:Topology
            home_assistant_version = $script:HaVersion
            providers_enabled    = $script:EnabledProviderTypes
            provider_reachability = $script:ProviderReachability
            files                = [ordered]@{
                installer_present    = (Test-Path -LiteralPath $InternalInstaller)
                profile_tool_present = (Test-Path -LiteralPath $ProfileTool)
                card_dist_present    = (Test-Path -LiteralPath $CardDist)
                manifest_present     = (Test-Path -LiteralPath (Join-Path $BundleRoot 'MANIFEST.sha256'))
            }
        }
        $reportJson = & $scrub ($report | ConvertTo-Json -Depth 6)
        [IO.File]::WriteAllText((Join-Path $reportDir 'support-report.json'), $reportJson)

        $logText = & $scrub (($script:Log -join "`n") + "`n")
        [IO.File]::WriteAllText((Join-Path $reportDir 'installer.log'), $logText)

        $envText = & $scrub (@(
            "os:            $([System.Runtime.InteropServices.RuntimeInformation]::OSDescription)",
            "powershell:    $($PSVersionTable.PSVersion)",
            "python:        $($Context['PythonVersion'])",
            "ssh_client:    $($Context['SshVersion'])",
            "scene_studio:  $Version",
            "install_kind:  $($script:InstallKind)"
        ) -join "`n") + "`n"
        [IO.File]::WriteAllText((Join-Path $reportDir 'environment.txt'), $envText)

        $probeText = & $scrub (($script:Probes -join "`n") + "`n")
        [IO.File]::WriteAllText((Join-Path $reportDir 'probes.txt'), $probeText)

        $zipPath = Join-Path ([IO.Path]::GetTempPath()) "scene-studio-support-$stamp.zip"
        if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath -Force }
        Compress-Archive -Path (Join-Path $reportDir '*') -DestinationPath $zipPath
        Remove-Item -LiteralPath $reportDir -Recurse -Force

        Write-Host ''
        Write-Host 'Scene Studio setup could not continue.' -ForegroundColor Red
        Write-Host ''
        Write-Host "Failed stage: $Stage"
        Write-Host "Reason: $Reason"
        Write-Host ''
        Write-Host 'Remote changes:'
        Write-Host "  $RemoteChangeState"
        Write-Host ''
        Write-Host 'Support report:'
        Write-Host "  $zipPath"
        Write-Host ''
        Write-Host 'Send this file to the Scene Studio maintainer.'
        return $zipPath
    } catch {
        Write-WizardWarning "Could not build the support report: $($_.Exception.Message)"
        Write-Host ''
        Write-Host "Scene Studio setup could not continue. Failed stage: $Stage. Reason: $Reason"
        return $null
    }
}

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

function ConvertTo-BashSingleQuoted {
    param([Parameter(Mandatory = $true)][string]$Value)
    $singleQuote = [string][char]39
    $doubleQuote = [string][char]34
    return $Value.Replace($singleQuote, $singleQuote + $doubleQuote + $singleQuote + $doubleQuote + $singleQuote)
}

function Test-TcpReachable {
    param([string]$TargetHost, [int]$Port, [int]$TimeoutMs = 3000)
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $task = $client.ConnectAsync($TargetHost, $Port)
        if (-not $task.Wait($TimeoutMs)) { return $false }
        return $client.Connected
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Get-PlainSecure {
    param([securestring]$Secure)
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

function Invoke-WizardSsh {
    param([Parameter(Mandatory = $true)][string]$Command)
    $args_ = @()
    if ($script:ResolvedSshPort -gt 0) { $args_ += @('-p', [string]$script:ResolvedSshPort) }
    $args_ += @('-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-o', 'StrictHostKeyChecking=accept-new')
    $result = & ssh @args_ $script:ResolvedSshDestination $Command
    if ($LASTEXITCODE -ne 0) {
        throw "ssh command failed (exit $LASTEXITCODE): $($Command.Substring(0, [Math]::Min(120, $Command.Length)))"
    }
    return ($result -join "`n")
}

function Send-RemoteText {
    param([Parameter(Mandatory = $true)][string]$RemotePath, [Parameter(Mandatory = $true)][string]$Text)
    $quoted = ConvertTo-BashSingleQuoted $RemotePath
    $bytes = [Text.Encoding]::UTF8.GetBytes($Text)
    $args_ = @()
    if ($script:ResolvedSshPort -gt 0) { $args_ += @('-p', [string]$script:ResolvedSshPort) }
    $args_ += @('-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=accept-new')
    [Convert]::ToBase64String($bytes) | & ssh @args_ $script:ResolvedSshDestination "base64 -d | sudo tee '$quoted' > /dev/null"
    if ($LASTEXITCODE -ne 0) {
        throw "Failed writing remote file: $RemotePath"
    }
}

function Read-RemoteText {
    # Returns $null when the file does not exist.
    param([Parameter(Mandatory = $true)][string]$RemotePath)
    $quoted = ConvertTo-BashSingleQuoted $RemotePath
    $args_ = @()
    if ($script:ResolvedSshPort -gt 0) { $args_ += @('-p', [string]$script:ResolvedSshPort) }
    $args_ += @('-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-o', 'StrictHostKeyChecking=accept-new')
    $probe = & ssh @args_ $script:ResolvedSshDestination "sudo test -f '$quoted' && echo present || echo absent"
    if ($probe -notmatch 'present') { return $null }
    $encoded = & ssh @args_ $script:ResolvedSshDestination "sudo cat '$quoted' | base64 -w0"
    if ($LASTEXITCODE -ne 0) {
        throw "Failed reading remote file: $RemotePath"
    }
    $joined = ($encoded -join '').Trim()
    if (-not $joined) { return '' }
    return [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($joined))
}

function Invoke-ChildInstaller {
    # Run the validated internal installer as a child pwsh process, echoing
    # its output live while capturing it for the support log.
    param([hashtable]$Parameters)
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $InternalInstaller)
    foreach ($key in $Parameters.Keys) {
        $value = $Parameters[$key]
        if ($value -is [bool]) {
            if ($value) { $argList += "-$key" }
        } else {
            $argList += @("-$key", [string]$value)
        }
    }
    Add-WizardLog ("child installer: " + ($argList -join ' '))
    $output = & pwsh @argList 2>&1
    $exitCode = $LASTEXITCODE
    foreach ($line in @($output)) {
        $text = if ($line -is [System.Management.Automation.ErrorRecord]) { $line.ToString() } else { "$line" }
        Write-Host $text
        Add-WizardLog "  [installer] $text"
    }
    if ($exitCode -ne 0) {
        throw "The Scene Studio deployment failed (installer exit code $exitCode; see the output above)."
    }
}

function Get-Answer {
    # Prompt with a default, or take the value from the answers document.
    # Key may be dotted ('providers.hue.host') to walk the answers object.
    param([string]$Key, [string]$Prompt, [string]$Default = '', [switch]$Required)
    if ($script:AnswersData) {
        $node = $script:AnswersData
        $found = $true
        foreach ($segment in $Key.Split('.')) {
            if ($node -and $node.PSObject.Properties.Name -contains $segment) {
                $node = $node.$segment
            } else {
                $found = $false
                break
            }
        }
        if ($found -and $null -ne $node) {
            if ($node -is [bool]) { return $node }
            return [string]$node
        }
    }
    if ($script:Unattended) {
        if ($Default) { return $Default }
        if ($Required) { throw "Unattended run: answers file does not provide '$Key'." }
        return ''
    }
    $suffix = if ($Default) { " [$Default]" } else { '' }
    $value = Read-Host -Prompt "$Prompt$suffix"
    if (-not $value) { $value = $Default }
    if ($Required -and -not $value) {
        throw "A value is required for: $Prompt"
    }
    return $value
}

function Get-AnswerBool {
    param([string]$Key, [string]$Prompt, [bool]$Default)
    if ($script:AnswersData) {
        if ($script:AnswersData.PSObject.Properties.Name -contains $Key -and $null -ne $script:AnswersData.$Key) {
            return [bool]$script:AnswersData.$Key
        }
    }
    if ($script:Unattended) { return $Default }
    $reply = Read-Host -Prompt "$Prompt [$(if ($Default) { 'Y' } else { 'y' })/$(if ($Default) { 'n' } else { 'N' })]"
    if (-not $reply) { return $Default }
    return ($reply -match '^(y|yes)$')
}

# ---------------------------------------------------------------------------
# bounded apps.yaml mutation (deterministic text, no YAML parser)
# ---------------------------------------------------------------------------

$BlockStartMarker = '# >>> scene_studio managed block (generated by Install-SceneStudio.ps1) >>>'
$BlockEndMarker = '# <<< scene_studio managed block <<<'

function Find-SceneStudioBlockRange {
    # Returns @{Start;End} line indexes (inclusive) of the existing
    # scene_studio block in apps.yaml, or $null when absent. Marker-delimited
    # blocks (ours) are replaced exactly; a bare 'scene_studio:' key is
    # bounded by the next top-level mapping key.
    param([string[]]$Lines)
    for ($i = 0; $i -lt $Lines.Count; $i++) {
        if ($Lines[$i] -eq $BlockStartMarker) {
            for ($j = $i + 1; $j -lt $Lines.Count; $j++) {
                if ($Lines[$j] -eq $BlockEndMarker) {
                    return @{ Start = $i; End = $j }
                }
            }
            throw 'apps.yaml contains an unterminated scene_studio managed block; fix it manually.'
        }
    }
    for ($i = 0; $i -lt $Lines.Count; $i++) {
        if ($Lines[$i] -match '^scene_studio:\s*$') {
            $end = $Lines.Count - 1
            for ($j = $i + 1; $j -lt $Lines.Count; $j++) {
                if ($Lines[$j] -match '^[A-Za-z0-9_-]+:' -or $Lines[$j] -eq $BlockStartMarker) {
                    $end = $j - 1
                    break
                }
            }
            return @{ Start = $i; End = $end }
        }
    }
    return $null
}

function Merge-SceneStudioBlock {
    param([string]$OriginalText, [Parameter(Mandatory = $true)][string]$BlockText)
    $managed = @($BlockStartMarker) + ($BlockText -split "`r?`n") + @($BlockEndMarker)
    if ([string]::IsNullOrWhiteSpace($OriginalText)) {
        return (($managed -join "`n") + "`n")
    }
    $lines = @($OriginalText -split "`r?`n")
    # a trailing empty element from a final newline is not a content line
    if ($lines.Count -gt 0 -and $lines[$lines.Count - 1] -eq '') {
        $trailingNewline = $true
        $lines = $lines[0..($lines.Count - 2)]
    } else {
        $trailingNewline = $false
    }
    $range = Find-SceneStudioBlockRange -Lines $lines
    $before = @()
    $after = @()
    if ($null -ne $range) {
        if ($range.Start -gt 0) { $before = $lines[0..($range.Start - 1)] }
        if ($range.End -lt $lines.Count - 1) { $after = $lines[($range.End + 1)..($lines.Count - 1)] }
    } else {
        $before = $lines
        # blank separator between existing content and the appended block
        while ($before.Count -gt 0 -and $before[$before.Count - 1] -eq '') {
            $before = $before[0..($before.Count - 2)]
        }
        $before = @($before) + @('')
    }
    $merged = @($before + $managed + $after) | Where-Object { $true }
    $text = ($merged -join "`n")
    if ($trailingNewline -or -not $text.EndsWith("`n")) { $text += "`n" }
    return $text
}

# ---------------------------------------------------------------------------
# begin
# ---------------------------------------------------------------------------

Write-Host 'Scene Studio Setup' -ForegroundColor Green
Write-Host '==================='
Write-Host "Scene Studio $Version - lighting scenes for Home Assistant."
Write-Host 'This wizard installs Scene Studio onto your Home Assistant + AppDaemon'
Write-Host 'system. Your computer only needs PowerShell 7, Python, and ssh -'
Write-Host 'everything else runs on the target.'
Write-Host ''
Write-Host 'Where the answers come from (official docs):'
Write-Host '  HA address + access token:  https://www.home-assistant.io/docs/authentication/'
Write-Host '  ssh add-on (key access):    https://github.com/hassio-addons/app-ssh'
Write-Host '  AppDaemon add-on:           https://github.com/hassio-addons/addon-appdaemon'
if (-not $Unattended) {
    Read-Host 'Press Enter to begin'
}

$script:Stage = 'workstation-checks'
Write-Step '1/9 Checking this computer (deployment workstation)'
foreach ($dep in @(
    @{ Name = 'python'; Hint = 'install Python 3.9+ from https://python.org' },
    @{ Name = 'ssh'; Hint = $(if ($IsWindows) { 'install OpenSSH client (Windows: Settings > Apps > Optional Features)' } else { 'install the OpenSSH client (macOS ships one; Linux: your package manager openssh-client)' }) }
)) {
    $cmd = Get-Command $dep.Name -ErrorAction SilentlyContinue
    if (-not $cmd) {
        throw "Missing dependency: $($dep.Name). $($dep.Hint)"
    }
    Write-Detail "$($dep.Name): ok"
}
$pythonVersion = (& python --version) 2>&1
$SshVersion = Get-SshClientVersion
Write-Detail "$pythonVersion"
Write-Detail "ssh: $SshVersion"
Record-Probe 'python' "$pythonVersion"

. (Join-Path (Join-Path $BundleRoot 'installer') 'topology.ps1')
$script:Topology = $null

# ---------------------------------------------------------------------------
# main flow — every failure lands in the support-report handler below
# ---------------------------------------------------------------------------

try {
    if ($ProfilePath) {
        $script:Stage = 'advanced-profile'
        Write-Step 'Advanced profile path: running the existing installer'
        # The internal installer takes only Profile/Apply (+ target overrides);
        # wizard-only switches (Yes/Unattended/Answers) are not forwarded.
        $parameters = @{ Profile = $ProfilePath }
        if ($Apply) { $parameters.Apply = $true }
        Invoke-ChildInstaller -Parameters $parameters
        exit 0
    }

    if ($Unattended) {
        if (-not $Answers -or -not (Test-Path -LiteralPath $Answers)) {
            throw '-Unattended requires -Answers pointing to an answers JSON file.'
        }
        $script:AnswersData = Get-Content -LiteralPath $Answers -Raw | ConvertFrom-Json
    } else {
        $script:AnswersData = $null
    }

    # --- 2. Home Assistant connection ------------------------------------
    $script:Stage = 'ha-api'
    Write-Step '2/9 Home Assistant API connection'
    Write-Detail 'The address is the same one you use to open Home Assistant in a browser.'
    $HaUrl = (Get-Answer -Key 'ha_url' -Prompt 'Home Assistant API address' -Default 'http://homeassistant.local:8123' -Required).TrimEnd('/')
    try { $script:Topology = New-InstallerTopology -HaUrl $HaUrl } catch { $HaUrl = ''; throw }
    Write-Detail "Testing $HaUrl ..."
    try {
        # Any HTTP answer (even 401) proves reachability; only a connection
        # failure means the address is wrong or unreachable.
        $null = Invoke-WebRequest -Uri "$HaUrl/api/" -Method Get -TimeoutSec 8 -SkipCertificateCheck -ErrorAction Stop
        Record-Probe 'ha_api_http' 'reachable'
        Write-Detail 'reachable'
    } catch {
        $status = $null
        try { $status = [int]$_.Exception.Response.StatusCode } catch { }
        if (-not $status) {
            Record-Probe 'ha_api_http' "unreachable: $($_.Exception.Message)"
            throw "Home Assistant is not reachable at $HaUrl : $($_.Exception.Message)"
        }
        Record-Probe 'ha_api_http' "reachable (HTTP $status)"
        Write-Detail "reachable (HTTP $status, authentication required as expected)"
    }
    $HaToken = $env:SCENE_STUDIO_HA_TOKEN
    if (-not $HaToken -and -not $Unattended) {
        Write-Detail 'A long-lived access token lets the installer verify health and restart AppDaemon.'
        Write-Detail 'Create one in Home Assistant: click your profile (bottom left) > Security > Long-lived access tokens'
        Write-Detail '(https://www.home-assistant.io/docs/authentication/).'
        $secure = Read-Host -Prompt 'Paste the token' -AsSecureString
        $HaToken = Get-PlainSecure $secure
    }
    if (-not $HaToken) {
        throw ('A Home Assistant long-lived access token is required (SCENE_STUDIO_HA_TOKEN, or paste it when prompted). ' +
            'It is used only for this installation and is not saved to disk. How to create one: ' +
            'https://www.home-assistant.io/docs/authentication/')
    }
    try {
        $config = Invoke-RestMethod -Uri "$HaUrl/api/config" -Method Get -Headers @{ Authorization = "Bearer $HaToken" } -TimeoutSec 10
        $script:HaVersion = [string]$config.version
        Record-Probe 'ha_api_auth' "ok (HA $($script:HaVersion))"
        Write-Detail "authenticated; Home Assistant $($script:HaVersion)"
    } catch {
        Record-Probe 'ha_api_auth' "rejected: $($_.Exception.Message)"
        throw "The token was rejected by $HaUrl/api/config : $($_.Exception.Message) Create a fresh token per https://www.home-assistant.io/docs/authentication/."
    }

    # --- 3. automatic/common SSH filesystem topology ----------------------
    $script:Stage = 'topology'
    Write-Step '3/9 Detecting Home Assistant filesystem access'
    Write-Detail 'The installer accesses add-on files through SSH to the Home Assistant filesystem host.'
    Write-Detail 'Key authentication setup: https://github.com/hassio-addons/app-ssh'
    function Get-TopologyAnswer([string]$Key, $Default) {
        if ($script:AnswersData -and $script:AnswersData.PSObject.Properties.Name -contains $Key -and $null -ne $script:AnswersData.$Key) { return $script:AnswersData.$Key }
        return $Default
    }
    $SshHostInput = [string](Get-TopologyAnswer 'ssh_host' ([Uri]$HaUrl).DnsSafeHost)
    if (-not $SshHostInput) { $SshHostInput = ([Uri]$HaUrl).DnsSafeHost }
    $SshUser = [string](Get-TopologyAnswer 'ssh_user' '')
    $ResolvedSshPort = [int](Get-TopologyAnswer 'ssh_port' $(if ($SshPort -gt 0) { $SshPort } else { 22 }))
    $script:Topology = New-InstallerTopology -HaUrl $HaUrl -HostName $SshHostInput -User $SshUser -Port $ResolvedSshPort
    $sshOk = Test-InstallerSsh $script:Topology
    if (-not $sshOk) {
        Write-Detail 'Automatic setup detection was incomplete. Configure advanced topology.'
        if ($Unattended) {
            throw 'SSH filesystem access failed. Configure advanced topology with ssh_host, ssh_user and ssh_port in the answers file; no files were changed.'
        }
        $SshHostInput = Get-Answer -Key 'ssh_host' -Prompt 'Home Assistant filesystem host (SSH)' -Default $SshHostInput -Required
        $SshUser = Get-Answer -Key 'ssh_user' -Prompt 'SSH username (empty = SSH default)' -Default $SshUser
        $portInput = Read-Host -Prompt "SSH port [$ResolvedSshPort]"
        if ($portInput) { $ResolvedSshPort = [int]$portInput }
        $script:Topology = New-InstallerTopology -HaUrl $HaUrl -HostName $SshHostInput -User $SshUser -Port $ResolvedSshPort
        $sshOk = Test-InstallerSsh $script:Topology
    }
    $ResolvedSshDestination = if ($SshUser) { "$SshUser@$SshHostInput" } else { $SshHostInput }
    Record-Probe 'ssh' "$(if ($sshOk) { 'ok' } else { 'failed' }) $ResolvedSshDestination port $ResolvedSshPort"
    if (-not $sshOk) { throw "SSH filesystem access to $ResolvedSshDestination failed; check key authentication and sudo access." }
    $script:Topology.capabilities.ssh_filesystem_available = $true
    Write-Detail 'SSH filesystem access available'
    $restartOk = Test-InstallerSupervisor -HaUrl $HaUrl -Token $HaToken
    $script:Topology.capabilities.supervisor_restart_available = $restartOk
    if ($restartOk) { $script:Topology.appdaemon_restart_strategy = 'supervisor' }
    Record-Probe 'supervisor_restart' "available=$restartOk"

    # --- 4. AppDaemon configuration directory ------------------------------
    $script:Stage = 'appdaemon-root'
    Write-Step '4/9 AppDaemon configuration directory'
    $AddonRoot = [string](Get-TopologyAnswer 'appdaemon_config_root' '')
    if (-not $AddonRoot) {
        Write-Detail 'Auto-detecting AppDaemon under /addon_configs ...'
        $candidates = @()
        try {
            $names = (Invoke-WizardSsh -Command "ls -1 '/addon_configs'") -split "`r?`n" | Where-Object { $_ }
            foreach ($name in $names) {
                $candidate = "/addon_configs/$name"
                $q = ConvertTo-BashSingleQuoted $candidate
                try {
                    $null = Invoke-WizardSsh -Command "test -f '$q/appdaemon.yaml' || test -d '$q/apps'"
                    $candidates += $candidate
                } catch { continue }
            }
        } catch {
            Write-Detail "auto-detection unavailable ($($_.Exception.Message.Split("`n")[0]))"
        }
        Record-Probe 'appdaemon_root_autodetect' "candidates=$($candidates.Count)"
        if ($candidates.Count -eq 1) {
            $AddonRoot = $candidates[0]
            Write-Detail "found exactly one candidate: $AddonRoot"
        } elseif ($candidates.Count -gt 1) {
            Write-Detail 'Several AppDaemon installations were found:'
            for ($i = 0; $i -lt $candidates.Count; $i++) {
                Write-Host "      [$($i + 1)] $($candidates[$i])"
            }
            if ($script:AnswersData -and $script:AnswersData.PSObject.Properties.Name -contains 'addon_root_choice' -and $script:AnswersData.addon_root_choice) {
                $choice = [int]$script:AnswersData.addon_root_choice
                if ($choice -lt 1 -or $choice -gt $candidates.Count) {
                    throw "addon_root_choice $choice is out of range (1..$($candidates.Count))."
                }
                $AddonRoot = $candidates[$choice - 1]
            } elseif ($Unattended) {
                throw 'Multiple AppDaemon candidates found; provide appdaemon_config_root (or addon_root_choice) in the answers file.'
            } else {
                $picked = 0
                while ($picked -lt 1 -or $picked -gt $candidates.Count) {
                    $picked = [int](Read-Host -Prompt 'Select the AppDaemon to use (number)')
                }
                $AddonRoot = $candidates[$picked - 1]
            }
            Write-Detail "selected: $AddonRoot"
        } else {
            Write-Detail 'no candidates detected under /addon_configs'
            if ($Unattended) {
                throw 'Automatic setup detection was incomplete. Configure advanced topology: provide ssh_host and appdaemon_config_root in the answers file.'
            }
            Write-Detail 'Automatic setup detection was incomplete. Configure advanced topology.'
            # SSH to HA may work even when AppDaemon files live elsewhere.
            $SshHostInput = Get-Answer -Key 'ssh_host' -Prompt 'Home Assistant filesystem host (SSH)' -Default $SshHostInput -Required
            $SshUser = Get-Answer -Key 'ssh_user' -Prompt 'SSH username (empty = SSH default)' -Default $SshUser
            $portInput = Read-Host -Prompt "SSH port [$ResolvedSshPort]"
            if ($portInput) { $ResolvedSshPort = [int]$portInput }
            $script:Topology = New-InstallerTopology -HaUrl $HaUrl -HostName $SshHostInput -User $SshUser -Port $ResolvedSshPort
            if (-not (Test-InstallerSsh $script:Topology)) { throw 'Advanced SSH filesystem access failed; nothing was changed.' }
            $ResolvedSshDestination = if ($SshUser) { "$SshUser@$SshHostInput" } else { $SshHostInput }
            $script:Topology.capabilities.ssh_filesystem_available = $true
            $script:Topology.capabilities.supervisor_restart_available = $restartOk
            if ($restartOk) { $script:Topology.appdaemon_restart_strategy = 'supervisor' }
            Write-Detail 'On Home Assistant OS the AppDaemon add-on config lives under /addon_configs/<add-on slug>'
            Write-Detail '(https://github.com/hassio-addons/addon-appdaemon).'
            $AddonRoot = Read-Host -Prompt 'Enter the AppDaemon config directory (e.g. /addon_configs/a0d7b954_appdaemon)'
        }
    }
    if ($AddonRoot -notmatch '^/' -or $AddonRoot -match '\.\.') {
        throw "AppDaemon config directory must be an absolute path: $AddonRoot"
    }
    $quotedRoot = ConvertTo-BashSingleQuoted $AddonRoot
    $rootProbe = Invoke-WizardSsh -Command "test -d '$quotedRoot' && echo addon_root=present || echo addon_root=absent"
    Record-Probe 'appdaemon_root' "$AddonRoot $(if ($rootProbe -match 'addon_root=absent') { 'ABSENT' } else { 'present' })"
    if ($rootProbe -match 'addon_root=absent') {
        throw "AppDaemon config directory '$AddonRoot' does not exist on the target. Check the path (find it under /addon_configs on the HA host)."
    }
    Write-Detail "using $AddonRoot"

    # --- 3b. Scene Studio / AppDaemon HTTP endpoint -----------------------
    $script:Stage = 'appdaemon-endpoint'
    Write-Step '3b/9 Scene Studio HTTP endpoint (AppDaemon)'
    $explicitHttp = ''
    if ($AppDaemonHttpUrl) {
        $explicitHttp = $AppDaemonHttpUrl.TrimEnd('/')
    } else {
        $answered = Get-TopologyAnswer 'appdaemon_http_url' ''
        if ($answered) { $explicitHttp = $answered.TrimEnd('/') }
    }
    if ($explicitHttp) {
        $AppDaemonHttp = $explicitHttp
    } else {
        # Infer from the SSH TARGET (not the HA API host): HA Core and
        # AppDaemon do not have to share a machine or hostname.
        $inferredHost = $SshHostInput -replace '^.*@', ''
        if ($inferredHost.Contains(':') -and -not $inferredHost.StartsWith('[')) { $inferredHost = "[$inferredHost]" }
        $AppDaemonHttp = "http://${inferredHost}:5050"
        Write-Detail "inferred $AppDaemonHttp from the ssh target + the default AppDaemon port"
    }
    try { $httpUri = Get-InstallerHttpUri $AppDaemonHttp } catch { $AppDaemonHttp = ''; throw }
    if ($explicitHttp) { Write-Detail "using the provided Scene Studio HTTP endpoint: $AppDaemonHttp" }
    $httpPort = if ($httpUri.Port -gt 0) { $httpUri.Port } else { if ($httpUri.Scheme -eq 'https') { 443 } else { 80 } }
    $httpReachable = Test-TcpReachable -TargetHost $httpUri.Host -Port $httpPort
    Record-Probe 'appdaemon_http_tcp' "$AppDaemonHttp $(if ($httpReachable) { 'reachable' } else { 'unreachable' })"
    if ($httpReachable) {
        Write-Detail "reachable (tcp/$httpPort)"
    } else {
        if ($explicitHttp) {
            Write-WizardWarning "$AppDaemonHttp is not reachable from this computer right now (firewall?). Continuing; the Workbench URL will use it."
        } elseif (-not $Unattended) {
            Write-Detail 'Automatic setup detection was incomplete. Configure advanced topology.'
            Write-Detail 'The inferred address is not reachable. Home Assistant and AppDaemon do NOT have to share a host.'
            Write-Detail 'AppDaemon serves HTTP on its dashboard port (default 5050):'
            Write-Detail '  https://appdaemon.readthedocs.io/en/latest/ADDON.html'
            $manual = (Read-Host -Prompt 'Scene Studio / AppDaemon HTTP address (e.g. http://appdaemon-host:5050)').TrimEnd('/')
            if (-not $manual) {
                throw 'A Scene Studio HTTP endpoint is required (the Workbench URL and health check come from it).'
            }
            $AppDaemonHttp = $manual
            try { $httpUri = Get-InstallerHttpUri $AppDaemonHttp } catch { $AppDaemonHttp = ''; throw }
            $httpPort = if ($httpUri.Port -gt 0) { $httpUri.Port } else { 80 }
            $httpReachable = Test-TcpReachable -TargetHost $httpUri.Host -Port $httpPort
            Record-Probe 'appdaemon_http_tcp_manual' "$AppDaemonHttp $(if ($httpReachable) { 'reachable' } else { 'unreachable' })"
            if ($httpReachable) { Write-Detail 'reachable' } else { Write-WizardWarning "still not reachable; continuing with $AppDaemonHttp" }
        } else {
            throw (
                "The inferred Scene Studio HTTP endpoint $AppDaemonHttp is not reachable from this computer. " +
                "Home Assistant and AppDaemon do not have to share a host: add 'appdaemon_http_url' to the answers file."
            )
        }
    }

    try { $httpUri = Get-InstallerHttpUri $AppDaemonHttp } catch { $AppDaemonHttp = ''; throw }
    $script:Topology.appdaemon_config_root = $AddonRoot
    $script:Topology.appdaemon_http_url = $AppDaemonHttp
    $script:Topology.capabilities.appdaemon_root_detected = $true
    $script:Topology.capabilities.appdaemon_http_reachable = [bool]$httpReachable
    $splitConfirmed = Get-TopologyAnswer 'ha_config_filesystem_confirmed' $false
    if ($splitConfirmed -isnot [bool]) { throw 'ha_config_filesystem_confirmed must be a JSON boolean.' }
    $script:Topology.capabilities.ha_www_access_available = Test-InstallerHaWww -Topology $script:Topology -HaVersion $script:HaVersion -ConfirmedSplitFilesystem $splitConfirmed
    Record-Probe 'ha_www_access' "available=$($script:Topology.capabilities.ha_www_access_available)"
    if ($httpReachable -and $restartOk) {
        Write-Detail 'Detected setup: API reachable; SSH filesystem access available; AppDaemon add-on found; AppDaemon HTTP reachable; restart capability available.'
    } else {
        Write-Detail 'Automatic setup detection was incomplete. Advanced topology is shown in the review.'
    }

    # --- 5. providers -------------------------------------------------------
    $script:Stage = 'providers'
    Write-Step '5/9 Lighting sources Scene Studio should discover'
    function Get-ProviderEnabled {
        param([string]$Key, [string]$Label, [bool]$Default)
        if ($script:AnswersData -and $script:AnswersData.PSObject.Properties.Name -contains 'providers') {
            $providers = $script:AnswersData.providers
            if ($providers.PSObject.Properties.Name -contains $Key -and $null -ne $providers.$Key) {
                if ($Key -eq 'ha_light') { return [bool]$providers.$Key }
                return [bool]$providers.$Key.enabled
            }
        }
        if ($Unattended) { return $Default }
        $reply = Read-Host -Prompt "$Label [$(if ($Default) { 'Y' } else { 'y' })/$(if ($Default) { 'n' } else { 'N' })]"
        if (-not $reply) { return $Default }
        return ($reply -match '^(y|yes)$')
    }

    $UseHaLight = Get-ProviderEnabled -Key 'ha_light' -Label 'Home Assistant lights' -Default $true
    $UseHue = Get-ProviderEnabled -Key 'hue' -Label 'Philips Hue' -Default $false
    $UseWled = Get-ProviderEnabled -Key 'wled' -Label 'WLED' -Default $false
    $UseHyperhdr = Get-ProviderEnabled -Key 'hyperhdr' -Label 'hyperHDR integration (external light-sync contention)' -Default $false

    $HueHost = ''; $HueBridgeId = ''; $WledHost = ''; $HyperhdrHost = ''
    $HueAppKey = $env:SCENE_STUDIO_HUE_APP_KEY
    if ($UseHue) {
        Write-Detail 'Hue docs: find the bridge address in the Hue app (Settings > My Hue bridge):'
        Write-Detail '  https://developers.meethue.com/develop/get-started-2/'
        $HueHost = Get-Answer -Key 'providers.hue.host' -Prompt 'Hue bridge address' -Default '' -Required
        $hostPort = if ($HueHost -match '^([^:]+):(\d+)$') { @{ h = $Matches[1]; p = [int]$Matches[2] } } else { @{ h = $HueHost; p = 443 } }
        $hueOk = Test-TcpReachable -TargetHost $hostPort.h -Port $hostPort.p
        $script:ProviderReachability += "hue=$($hostPort.h):$($hostPort.p) $(if ($hueOk) { 'reachable' } else { 'UNREACHABLE' })"
        if (-not $hueOk) {
            throw "Hue bridge $HueHost is not reachable on tcp/$($hostPort.p)."
        }
        Write-Detail "Hue bridge reachable ($($hostPort.h):$($hostPort.p))"
        if (-not $HueAppKey) {
            if ($Unattended) {
                throw 'Hue is enabled but SCENE_STUDIO_HUE_APP_KEY is not set (unattended runs never prompt).'
            }
            Write-Detail 'A Hue application key authorizes Scene Studio on your bridge.'
            Write-Detail 'Press the LINK button on the bridge, then press Enter here to pair automatically.'
            $pair = Read-Host -Prompt 'Ready to pair? (Enter = try pairing, m = I have a key, s = skip Hue)'
            if ($pair -match '^(m|M)$') {
                $secure = Read-Host -Prompt 'Paste the application key' -AsSecureString
                $HueAppKey = Get-PlainSecure $secure
            } elseif ($pair -match '^(s|S)$') {
                $UseHue = $false
            } else {
                try {
                    $pairBody = @{ devicetype = 'scene_studio'; generatekey = $true } | ConvertTo-Json -Compress
                    $pairResult = Invoke-RestMethod -Uri "https://$($hostPort.h):$($hostPort.p)/api" -Method Post -Body $pairBody -ContentType 'application/json' -TimeoutSec 15 -SkipCertificateCheck
                    if ($pairResult[0].error) { throw $pairResult[0].error.description }
                    $HueAppKey = $pairResult[0].success.username
                    Write-Detail 'paired with the bridge'
                } catch {
                    Write-WizardWarning "Automatic pairing failed: $($_.Exception.Message)"
                    $secure = Read-Host -Prompt 'Paste the application key (or press Enter to skip Hue)' -AsSecureString
                    $HueAppKey = Get-PlainSecure $secure
                    if (-not $HueAppKey) { $UseHue = $false }
                }
            }
        }
        if ($UseHue -and $HueAppKey) {
            # Best-effort bridge id (used for first-run fixture adoption).
            try {
                $bridgeInfo = Invoke-RestMethod -Uri "https://$($hostPort.h):$($hostPort.p)/api/config" -TimeoutSec 8 -SkipCertificateCheck
                if ($bridgeInfo.bridgeid) {
                    $HueBridgeId = [string]$bridgeInfo.bridgeid
                    Write-Detail "bridge id $($HueBridgeId)"
                }
            } catch {
                Write-Detail 'bridge id not detected (it can be added to apps.yaml later)'
            }
        }
    }
    if ($UseWled) {
        Write-Detail 'WLED docs: the controller address is its web UI address, e.g. http://wled-1234.local'
        Write-Detail '  https://kno.wled.ge/basics/web-ui/'
        $WledHost = Get-Answer -Key 'providers.wled.host' -Prompt 'WLED controller address' -Default '' -Required
        $hostPort = if ($WledHost -match '^([^:]+):(\d+)$') { @{ h = $Matches[1]; p = [int]$Matches[2] } } else { @{ h = $WledHost; p = 80 } }
        $wledOk = Test-TcpReachable -TargetHost $hostPort.h -Port $hostPort.p
        $script:ProviderReachability += "wled=$($hostPort.h):$($hostPort.p) $(if ($wledOk) { 'reachable' } else { 'UNREACHABLE' })"
        if (-not $wledOk) {
            throw "WLED controller $WledHost is not reachable on tcp/$($hostPort.p)."
        }
        Write-Detail "WLED reachable ($($hostPort.h):$($hostPort.p))"
    }
    if ($UseHyperhdr) {
        Write-Detail 'hyperHDR docs (address is host:port, e.g. tv.local:8090):'
        Write-Detail '  https://github.com/awawa-dev/HyperHDR'
        $HyperhdrHost = Get-Answer -Key 'providers.hyperhdr.host' -Prompt 'hyperHDR address (host:port)' -Default '' -Required
        $hostPort = if ($HyperhdrHost -match '^([^:]+):(\d+)$') { @{ h = $Matches[1]; p = [int]$Matches[2] } } else { throw 'hyperHDR address must be host:port' }
        $hdrOk = Test-TcpReachable -TargetHost $hostPort.h -Port $hostPort.p
        $script:ProviderReachability += "hyperhdr=$($hostPort.h):$($hostPort.p) $(if ($hdrOk) { 'reachable' } else { 'UNREACHABLE' })"
        if (-not $hdrOk) {
            throw "hyperHDR $HyperhdrHost is not reachable on tcp/$($hostPort.p)."
        }
        Write-Detail "hyperHDR reachable ($($hostPort.h):$($hostPort.p))"
    }
    $enabledList = @()
    $script:EnabledProviderTypes = @()
    if ($UseHaLight) { $enabledList += 'HA lights'; $script:EnabledProviderTypes += 'ha_light' }
    if ($UseHue) { $enabledList += 'Hue'; $script:EnabledProviderTypes += 'hue' }
    if ($UseWled) { $enabledList += 'WLED'; $script:EnabledProviderTypes += 'wled' }
    if ($UseHyperhdr) { $enabledList += 'hyperHDR'; $script:EnabledProviderTypes += 'hyperhdr' }
    if ($enabledList.Count -eq 0) {
        throw 'At least one lighting source must be enabled.'
    }
    Write-Detail "enabled: $($enabledList -join ', ')"

    # --- 6. remote state + generated configuration --------------------------
    $script:Stage = 'remote-state'
    Write-Step '6/9 Reading the target (read-only)'
    $BackendTarget = "$AddonRoot/apps/scene_studio"
    $WorkbenchTarget = "$AddonRoot/www/scene_studio"
    $stateProbe = Invoke-WizardSsh -Command (
        "if test -d '$(ConvertTo-BashSingleQuoted $BackendTarget)'; then echo backend=present; else echo backend=absent; fi; " +
        "if test -d '$(ConvertTo-BashSingleQuoted $WorkbenchTarget)'; then echo workbench=present; else echo workbench=absent; fi"
    )
    $IsUpgrade = ($stateProbe -match 'backend=present') -or ($stateProbe -match 'workbench=present')
    $script:InstallKind = if ($IsUpgrade) { 'upgrade' } else { 'fresh' }
    $AppsYamlPath = "$AddonRoot/apps.yaml"
    $SecretsYamlPath = "$AddonRoot/secrets.yaml"
    $appsProbe = Invoke-WizardSsh -Command "test -f '$(ConvertTo-BashSingleQuoted $AppsYamlPath)' && echo a=present || echo a=absent"
    $secretsProbe = Invoke-WizardSsh -Command "test -f '$(ConvertTo-BashSingleQuoted $SecretsYamlPath)' && echo s=present || echo s=absent"
    $HasAppsYaml = ($appsProbe -match 'a=present')
    $HasSecretsYaml = ($secretsProbe -match 's=present')
    $StoreRoot = Get-Answer -Key 'store_root' -Prompt 'Scene Studio data directory on the target' -Default '/config/scene_studio_store'
    if ($IsUpgrade) {
        # Preserve the currently configured runtime mode on upgrades.
        $RuntimeMode = 'registry_admin'
        try {
            $apiJson = Invoke-WizardSsh -Command "curl -fsS --max-time 8 -X POST -H 'Content-Type: application/json' --data '{`"method`":`"GET`",`"path`":`"/status`"}' http://127.0.0.1:5050/api/appdaemon/scene_studio_api"
            $api = $apiJson | ConvertFrom-Json
            if ($api.body.runtime.mode) {
                $RuntimeMode = [string]$api.body.runtime.mode
                Write-Detail "existing install detected; runtime mode is '$RuntimeMode' (preserved)"
            }
        } catch {
            Write-Detail "existing install detected; its runtime mode is not readable right now - configuring '$RuntimeMode'"
        }
    } else {
        $RuntimeMode = 'registry_admin'
        Write-Detail 'fresh installation detected'
    }
    Write-Detail "apps.yaml: $(if ($HasAppsYaml) { 'present' } else { 'absent (will be created)' })"
    Write-Detail "secrets.yaml: $(if ($HasSecretsYaml) { 'present' } else { 'absent (will be created)' })"

    # generated deployment configuration (workstation temp dir; never in the release)
    $WorkDir = Join-Path ([IO.Path]::GetTempPath()) "scene-studio-install-$(Get-Date -Format 'yyyyMMddTHHmmss')-$([Guid]::NewGuid().ToString('N'))"
    New-Item -ItemType Directory -Path $WorkDir | Out-Null
    $GeneratedProfile = Join-Path $WorkDir 'scene-studio.profile.json'
    # NB: assigning `if (...) { @() }` straight into a hashtable value unrolls
    # the empty array to $null (pipeline semantics); build the lists first.
    $WledHosts = @()
    if ($UseWled) { $WledHosts = @($WledHost) }
    $profile = [ordered]@{
        ha_url              = $HaUrl
        ha_ssh_host         = $ResolvedSshDestination
        appdaemon_config_root = $AddonRoot
        store_root          = $StoreRoot
        runtime_mode        = $RuntimeMode
        providers           = [ordered]@{
            hue     = [ordered]@{
                enabled                = [bool]$UseHue
                host                   = if ($UseHue) { $HueHost } else { $null }
                application_key_secret = if ($UseHue) { 'scene_studio_hue_app_key' } else { $null }
                bridge_id              = if ($HueBridgeId) { $HueBridgeId } else { $null }
            }
            wled    = [ordered]@{
                enabled = [bool]$UseWled
                hosts   = $WledHosts
            }
            ha_light = [ordered]@{
                enabled            = [bool]$UseHaLight
                ignored_entity_ids = @()
            }
        }
        integrations        = [ordered]@{
            hyperhdr = [ordered]@{
                enabled        = [bool]$UseHyperhdr
                host           = if ($UseHyperhdr) { $HyperhdrHost } else { $null }
                default_policy = 'yield'
            }
        }
    }
    $profile | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $GeneratedProfile -Encoding utf8NoBOM
    & python $ProfileTool validate --profile $GeneratedProfile
    if ($LASTEXITCODE -ne 0) {
        throw 'The generated deployment configuration failed validation - please report this.'
    }
    $rendered = & python $ProfileTool render-apps-yaml --profile $GeneratedProfile
    $renderedText = ($rendered -join "`n")
    $notesIndex = $renderedText.IndexOf("`nNotes:")
    if ($notesIndex -lt 0) { $notesIndex = $renderedText.Length }
    $BlockText = $renderedText.Substring(0, $notesIndex).TrimEnd()

    # --- 7. review -----------------------------------------------------------
    $script:Stage = 'review'
    Write-Step '7/9 Review'
    Write-Host '    Deployment topology'
    Write-Host "    Home Assistant API:        $HaUrl"
    Write-Host "    Filesystem access:         SSH -> $ResolvedSshDestination port $ResolvedSshPort"
    Write-Host "    Restart:                   $(if ($restartOk) { 'Home Assistant Supervisor' } else { 'manual (automatic installation unavailable)' })"
    Write-Host "    Scene Studio HTTP:         $AppDaemonHttp"
    Write-Host "    AppDaemon config:          $AddonRoot"
    Write-Host '    In-app update executor:    apps/scene_studio_update_supervisor.py + apps/scene_studio_release.py'
    Write-Host '    Restart effect:            AppDaemon add-on only; companion resumes recovery after restart'
    Write-Host "    Scene Studio data:         $StoreRoot"
    Write-Host "    Deployment type:           $(if ($IsUpgrade) { 'upgrade of the existing install' } else { 'FRESH install' })"
    Write-Host "    Runtime mode:              $RuntimeMode$(if ($RuntimeMode -eq 'registry_admin') { '  (provider writes blocked until you deliberately enable them)' })"
    Write-Host "    Lighting sources:          $($enabledList -join ', ')"
    $InstallCard = $false
    if ($script:Topology.capabilities.ha_www_access_available) {
        $InstallCard = Get-AnswerBool -Key 'install_ha_card' -Prompt 'Install the Scene Studio Home Assistant dashboard card' -Default $true
    } else {
        Write-Detail 'HA dashboard card: automatic deployment unavailable; HA /config filesystem access is not confirmed.'
        Write-Detail 'Manual deployment: copy home-assistant/scene-studio-card/dist/scene-studio-card.js to your HA /config/www/scene-studio-card/; register /local/scene-studio-card/scene-studio-card.js as a JavaScript module, then use custom:scene-studio-card.'
    }
    Write-Host "    HA dashboard card:         $(if ($InstallCard) { "yes - deploys $CardWwwFile (no dashboard is modified)" } else { 'no' })"
    Write-Host ''
    Write-Host "    apps.yaml change (a clearly marked block; everything else is untouched):"
    Write-Host ''
    $BlockText -split "`n" | ForEach-Object { Write-Host "      $_" }
    Write-Host ''
    if ($UseHue) {
        Write-Host '    secrets.yaml: adds entry "scene_studio_hue_app_key" (value not shown, not printed anywhere).'
    }
    Write-Host '    The HA access token you pasted is used for this installation only; it is not written to any file.'
    $AppsYamlNew = $null
    if ($HasAppsYaml) {
        $original = Read-RemoteText -RemotePath $AppsYamlPath
        $AppsYamlNew = Merge-SceneStudioBlock -OriginalText $original -BlockText $BlockText
        if ($AppsYamlNew -eq $original) {
            Write-Host '    apps.yaml: already up to date - no change needed.'
            $AppsYamlNew = $null
        }
    }
    $Confirmed = $false
    if ($Yes -or ($script:AnswersData -and $script:AnswersData.PSObject.Properties.Name -contains 'confirm_install' -and $script:AnswersData.confirm_install)) {
        $Confirmed = $true
    } elseif ($Unattended) {
        Write-Host ''
        Write-Host 'REVIEW ONLY: the answers file did not confirm installation (confirm_install). Nothing was changed.'
        Write-Host "Generated configuration: $GeneratedProfile"
        exit 0
    } else {
        $reply = Read-Host -Prompt 'Type INSTALL to apply these changes'
        $Confirmed = ($reply -ceq 'INSTALL')
    }
    if (-not $Confirmed) {
        Write-Host 'Cancelled - nothing was changed.'
        exit 1
    }

    if (-not $restartOk) { throw 'Automatic installation requires Home Assistant Supervisor addon_restart capability. Manual restart targets are not supported by the current deployers; nothing was changed.' }

    # --- 8. configure + install -----------------------------------------------
    $script:Stage = 'configure-appdaemon'
    Write-Step '8/9 Configuring AppDaemon and installing'
    $BackupDir = "$AddonRoot/backups/scene-studio-config-$([DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ'))"
    $ConfigBackup = @{ AppsYaml = $null; SecretsYaml = $null; SecretsChanged = $false; AppsPath = $AppsYamlPath; SecretsPath = $SecretsYamlPath }
    try {
        # config mutation (bounded; original bytes kept locally AND remotely)
        if ($HasAppsYaml) {
            $original = Read-RemoteText -RemotePath $AppsYamlPath
            $ConfigBackup.AppsYaml = $original
            $qBackup = ConvertTo-BashSingleQuoted "$BackupDir/apps.yaml.before"
            $null = Invoke-WizardSsh -Command "sudo install -d -m 750 '$(ConvertTo-BashSingleQuoted $BackupDir)'"
            $null = Invoke-WizardSsh -Command "sudo cp -a '$(ConvertTo-BashSingleQuoted $AppsYamlPath)' '$qBackup'"
            if ($null -ne $AppsYamlNew) {
                Send-RemoteText -RemotePath $AppsYamlPath -Text $AppsYamlNew
                Write-Detail 'apps.yaml updated (scene_studio block)'
            } else {
                Write-Detail 'apps.yaml unchanged'
            }
        } else {
            Send-RemoteText -RemotePath $AppsYamlPath -Text ((Merge-SceneStudioBlock -OriginalText '' -BlockText $BlockText))
            Write-Detail 'apps.yaml created (scene_studio block)'
        }
        if ($UseHue -and $HueAppKey) {
            $existingSecrets = Read-RemoteText -RemotePath $SecretsYamlPath
            $hadSecretsFile = ($null -ne $existingSecrets)
            if ($hadSecretsFile -and $existingSecrets -match '(?m)^scene_studio_hue_app_key\s*:') {
                Write-Detail 'secrets.yaml already carries scene_studio_hue_app_key - value left untouched'
            } else {
                # $null original => the file did not exist; rollback removes it
                $ConfigBackup.SecretsYaml = $existingSecrets
                $ConfigBackup.SecretsChanged = $true
                if ($hadSecretsFile) {
                    $qBackupSecrets = ConvertTo-BashSingleQuoted "$BackupDir/secrets.yaml.before"
                    $null = Invoke-WizardSsh -Command "sudo cp -a '$(ConvertTo-BashSingleQuoted $SecretsYamlPath)' '$qBackupSecrets'"
                }
                $base = if ($hadSecretsFile) { $existingSecrets.TrimEnd() } else { '' }
                $newText = $base + "`n" + 'scene_studio_hue_app_key: "' + ($HueAppKey -replace '"', '\"') + '"' + "`n"
                Send-RemoteText -RemotePath $SecretsYamlPath -Text $newText
                $null = Invoke-WizardSsh -Command "sudo chmod 600 '$(ConvertTo-BashSingleQuoted $SecretsYamlPath)'"
                Write-Detail "$(if ($hadSecretsFile) { 'secrets.yaml updated' } else { 'secrets.yaml created' }) (value not displayed)"
            }
        }

        # deployment through the validated installer (backup/stage/verify/rollback)
        $env:SCENE_STUDIO_HA_URL = $HaUrl
        $env:SCENE_STUDIO_HA_TOKEN = $HaToken
        # NB: the internal installer takes ha_url from the PROFILE (no -HaUrl
        # parameter exists) - the generated profile already carries it.
        $installerParams = @{
            Profile = $GeneratedProfile
            Apply   = $true
            HaHost  = $ResolvedSshDestination
        }
        if ($ResolvedSshPort -gt 0) { $installerParams.SshPort = $ResolvedSshPort }
        $script:Stage = 'backend-deploy'
        Invoke-ChildInstaller -Parameters $installerParams
    } catch {
        # --- automatic configuration rollback -----------------------------------
        $installError = $_.Exception.Message
        Write-WizardWarning "Installation failed: $installError"
        Write-WizardWarning 'Restoring the previous AppDaemon configuration...'
        try {
            if ($null -ne $ConfigBackup.AppsYaml) {
                Send-RemoteText -RemotePath $ConfigBackup.AppsPath -Text $ConfigBackup.AppsYaml
                Write-Detail 'apps.yaml restored'
            } elseif (-not $HasAppsYaml) {
                $null = Invoke-WizardSsh -Command "sudo rm -f '$(ConvertTo-BashSingleQuoted $AppsYamlPath)'"
                Write-Detail 'created apps.yaml removed'
            }
            if ($ConfigBackup.SecretsChanged) {
                if ($null -eq $ConfigBackup.SecretsYaml) {
                    $null = Invoke-WizardSsh -Command "sudo rm -f '$(ConvertTo-BashSingleQuoted $ConfigBackup.SecretsPath)'"
                    Write-Detail 'created secrets.yaml removed'
                } else {
                    Send-RemoteText -RemotePath $ConfigBackup.SecretsPath -Text $ConfigBackup.SecretsYaml
                    Write-Detail 'secrets.yaml restored'
                }
            }
            $slug = Split-Path $AddonRoot -Leaf
            $null = Invoke-RestMethod -Uri "$HaUrl/api/services/hassio/addon_restart" -Method Post -Headers @{ Authorization = "Bearer $HaToken" } -ContentType 'application/json' -Body (@{ addon = $slug } | ConvertTo-Json -Compress) -TimeoutSec 20
            Write-Detail 'AppDaemon restarting with the restored configuration'
            Write-Host "A pre-change copy of your configuration is also kept on the target: $BackupDir"
            $script:RemoteChangeState = 'previous configuration restored successfully'
        } catch {
            $script:RemoteChangeState = "AUTOMATIC RESTORE FAILED - inspect the target manually (remote backup: $BackupDir)"
            Write-WizardWarning "Automatic restore failed: $($_.Exception.Message). Remote backup: $BackupDir"
        }
        throw "Scene Studio installation failed. Error: $installError"
    }

    # --- 8b. optional Home Assistant dashboard card ---------------------------
    $script:Stage = 'ha-card'
    if ($InstallCard) {
        Write-Step '8b/9 Scene Studio Home Assistant dashboard card (optional component)'
        try {
            if (-not (Test-Path -LiteralPath $CardDist)) {
                Write-WizardWarning "The prebuilt card was not found at $CardDist - skipping the card step (the core install is complete and unaffected)."
                Record-Probe 'ha_card_deploy' 'skipped: prebuilt card missing'
            } else {
                $null = Invoke-WizardSsh -Command "sudo install -d -m 755 '$(ConvertTo-BashSingleQuoted $CardWwwDir)'"
                Send-RemoteText -RemotePath $CardWwwFile -Text (Get-Content -LiteralPath $CardDist -Raw)
                Write-Detail "card deployed to $CardWwwFile (Scene Studio-owned path; no dashboard was modified)"
                Record-Probe 'ha_card_deploy' $CardWwwFile
                Write-Host ''
                Write-Host '    Register the card resource ONCE in Home Assistant:'
                Write-Host '      Settings > Dashboards > (top right) Resources > Add resource'
                Write-Host '      URL: /local/scene-studio-card/scene-studio-card.js'
                Write-Host ''
                Write-Host '    Then add a card with the minimal configuration:'
                Write-Host '      type: custom:scene-studio-card'
                Write-Host ''
                Write-Host '    (Legacy dashboards may keep using type: custom:test-bench-scene-controls-card -'
                Write-Host '     the same file registers that name as a compatibility alias.)'
            }
        } catch {
            # The card is optional: a failed card deploy must not fail the install.
            Write-WizardWarning "Card deployment failed (the core install is complete and unaffected): $($_.Exception.Message)"
            Record-Probe 'ha_card_deploy' "FAILED: $($_.Exception.Message)"
        }
    } else {
        Write-Detail 'HA dashboard card step skipped (not requested).'
    }

    # --- 9. done ----------------------------------------------------------------
    $script:Stage = 'health-check'
    Write-Step '9/9 Installation complete'
    $WorkbenchUrl = "$AppDaemonHttp/local/scene_studio/"
    try {
        $health = Invoke-RestMethod -Uri "$AppDaemonHttp/api/appdaemon/scene_studio_api" -Method Post -ContentType 'application/json' -Body (@{ method = 'GET'; path = '/status' } | ConvertTo-Json -Compress) -TimeoutSec 10
        $healthMode = ''
        try { $healthMode = [string]$health.body.runtime.mode } catch { }
        Record-Probe 'scene_studio_http_health' "ok mode=$healthMode"
        Write-Detail "Scene Studio API reachable at $AppDaemonHttp$(if ($healthMode) { " (mode=$healthMode)" })"
    } catch {
        Record-Probe 'scene_studio_http_health' "unreachable from this computer: $($_.Exception.Message)"
        Write-WizardWarning "Could not reach $AppDaemonHttp from this computer (the install itself was verified over ssh). If the page does not open, check any firewall between this computer and the AppDaemon port."
    }
    Write-Host ''
    Write-Host "  Scene Studio is installed and running in '$RuntimeMode' mode." -ForegroundColor Green
    Write-Host "  Open the Workbench:  $WorkbenchUrl"
    Write-Host '  The Setup view will guide you: run discovery, adopt your fixtures,'
    Write-Host '  create rooms, and author your first scene. Provider writes stay'
    Write-Host '  blocked until you deliberately enable them (the Setup view shows how).'
    Write-Host ''
    Write-Host "  Deployment configuration (workstation): $GeneratedProfile"
    Write-Host "  Configuration backups (target):         $BackupDir"
    if (-not $Unattended) {
        $open = Read-Host -Prompt 'Open the Workbench in your browser now? (y/N)'
        if ($open -match '^(y|yes)$') {
            Start-Process $WorkbenchUrl
        }
    }
} catch {
    $reason = $_.Exception.Message
    $haTokenForScrub = $null
    if (Test-Path -LiteralPath 'Variable:HaToken') { $haTokenForScrub = $HaToken }
    $hueKeyForScrub = $null
    if (Test-Path -LiteralPath 'Variable:HueAppKey') { $hueKeyForScrub = $HueAppKey }
    $haUrlValue = ''
    if (Test-Path -LiteralPath 'Variable:HaUrl') { $haUrlValue = $HaUrl }
    $sshTargetValue = ''
    if (Test-Path -LiteralPath 'Variable:ResolvedSshDestination') { $sshTargetValue = "$ResolvedSshDestination port $ResolvedSshPort" }
    $httpValue = ''
    if (Test-Path -LiteralPath 'Variable:AppDaemonHttp') { $httpValue = $AppDaemonHttp }
    $rootValue = ''
    if (Test-Path -LiteralPath 'Variable:AddonRoot') { $rootValue = $AddonRoot }
    Write-SupportReport -Stage $script:Stage -Reason $reason -RemoteChangeState $script:RemoteChangeState -Context @{
        HaToken       = $haTokenForScrub
        HueAppKey     = $hueKeyForScrub
        PythonVersion = $pythonVersion
        SshVersion    = $SshVersion
        HaApiUrl      = $haUrlValue
        SshTarget     = $sshTargetValue
        AppDaemonHttp = $httpValue
        AddonRoot     = $rootValue
    }
    exit 1
}
