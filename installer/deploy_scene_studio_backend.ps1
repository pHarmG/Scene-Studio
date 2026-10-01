<#
.SYNOPSIS
  Safely deploys the Scene Studio AppDaemon Python package only.

.DESCRIPTION
  Builds a hash manifest from backend/src/scene_studio, backs up
  the current AppDaemon package, stages and verifies the replacement, then
  atomically activates it and restarts only the AppDaemon add-on. A failed
  health check restores the prior package and restarts the add-on again.

  Run under PowerShell 7 (pwsh) ONLY - see the version guard below.
  This script never changes apps.yaml, Scene Studio stores/scenes, secrets,
  dashboards, static Workbench assets, or Home Assistant Core. A package
  deploy therefore must not change status().runtime.mode: the live mode is
  captured immediately before activation, and a post-restart mismatch is a
  failed deploy that triggers rollback. -VerifyOnly checks package
  equivalence and API health for whatever mode is currently live.
#>

[CmdletBinding()]
param(
    [switch]$Apply,
    [switch]$VerifyOnly,
    [switch]$FreshInstall,
    [string]$HaHost = 'HA',
    [int]$SshPort = 0,
    [string]$AddonConfigRoot = '/addon_configs/a0d7b954_appdaemon',
    [string]$HaUrl = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# PS 7 ONLY. Windows PowerShell 5.1 strips embedded double quotes when
# marshalling arguments to ssh.exe, which silently corrupts the curl JSON
# payloads this script sends through Invoke-HaSsh (observed 2026-09-25: the
# health-check body reached HA with its quotes stripped and every deploy
# failed with "Checking Scene Studio API status failed (exit code 22)").
# Always run: pwsh -NoProfile -ExecutionPolicy Bypass -File <this script> ...
if ($PSVersionTable.PSVersion.Major -lt 7) {
    throw (
        'PowerShell 7 (pwsh) is required: Windows PowerShell 5.1 strips embedded quotes when ' +
        'marshalling ssh arguments, corrupting the HA API payloads of this deployer. Run: ' +
        "pwsh -NoProfile -ExecutionPolicy Bypass -File $($MyInvocation.MyCommand.Path) -Apply"
    )
}

if ($Apply -and $VerifyOnly) {
    throw 'Choose either -Apply or -VerifyOnly, not both.'
}
if ($FreshInstall -and $VerifyOnly) {
    throw '-FreshInstall applies a FIRST install; it cannot be combined with -VerifyOnly.'
}
if ($AddonConfigRoot -notmatch '^/addon_configs/[^/]+$') {
    throw "AddonConfigRoot must be one AppDaemon add-on config directory, not a broad path: $AddonConfigRoot"
}

# Optional non-default ssh port: the destination stays -HaHost (an alias or
# user@host); when set, every ssh invocation carries -p <SshPort>.
$script:SshArgs = @()
if ($SshPort -gt 0) {
    $script:SshArgs = @('-p', [string]$SshPort)
}

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
# HA URL resolution (portable-safe; there is deliberately NO hardcoded
# private default):
#   1. explicit -HaUrl parameter (the portable installer passes this);
#   2. SCENE_STUDIO_HA_URL environment variable (set by the portable
#      installer from the validated deployment profile);
#   3. the gitignored local operator profile (.scene-studio.local.json,
#      key "ha_api_url") or a pre-existing HA_URL;
#   4. otherwise FAIL with an actionable message - a clean checkout must
#      never guess a private HA address.
$LocalProfilePath = Join-Path $RepoRoot '.scene-studio.local.json'
if (-not $HaUrl -and $env:SCENE_STUDIO_HA_URL) {
    $HaUrl = $env:SCENE_STUDIO_HA_URL
}
if (-not $HaUrl -and (Test-Path -LiteralPath $LocalProfilePath)) {
    try {
        $localProfile = Get-Content -LiteralPath $LocalProfilePath -Raw | ConvertFrom-Json
        if ($localProfile.PSObject.Properties.Name -contains 'ha_api_url' -and $localProfile.ha_api_url) {
            $HaUrl = [string]$localProfile.ha_api_url
        }
    } catch {
        Write-Warning "Could not parse $LocalProfilePath : $($_.Exception.Message)"
    }
}
if (-not $HaUrl -and $env:HA_URL) {
    $HaUrl = $env:HA_URL
}
if (-not $HaUrl) {
    throw (
        'No Home Assistant URL resolved. Pass -HaUrl <url>, set SCENE_STUDIO_HA_URL (the ' +
        'portable installer does this from the deployment profile), or create the gitignored ' +
        '.scene-studio.local.json (see .scene-studio.local.example.json) with "ha_api_url".'
    )
}
if (-not $env:HA_TOKEN -and $env:SCENE_STUDIO_HA_TOKEN) {
    $env:HA_TOKEN = $env:SCENE_STUDIO_HA_TOKEN
}
$env:HA_URL = $HaUrl
$HaBase = $HaUrl.TrimEnd('/')
$SourceRoot = Join-Path $RepoRoot 'backend\src\scene_studio'
$RemoteRoot = $AddonConfigRoot.TrimEnd('/')
$RemoteTarget = "$RemoteRoot/apps/scene_studio"
$AddonSlug = Split-Path $RemoteRoot -Leaf
$Stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')
$RemoteStage = "/tmp/scene-studio-backend-$Stamp"
$RemoteBackupDir = "$RemoteRoot/backups/scene-studio-backend-$Stamp"
$RemoteArchive = "$RemoteBackupDir/apps-scene_studio-before.tar.gz"
$RemotePrevious = "$RemoteBackupDir/live-before"
$RemoteFailed = "$RemoteBackupDir/failed-new"

function Assert-LastNativeCommand {
    param([Parameter(Mandatory = $true)][string]$Action)
    if ($LASTEXITCODE -ne 0) {
        throw "$Action failed (exit code $LASTEXITCODE)."
    }
}

function ConvertTo-BashSingleQuoted {
    param([Parameter(Mandatory = $true)][string]$Value)
    $singleQuote = [string][char]39
    $doubleQuote = [string][char]34
    return $Value.Replace($singleQuote, $singleQuote + $doubleQuote + $singleQuote + $doubleQuote + $singleQuote)
}

function Invoke-HaSsh {
    param(
        [Parameter(Mandatory = $true)][string]$Command,
        [Parameter(Mandatory = $true)][string]$Action
    )
    $result = & ssh @script:SshArgs $HaHost $Command
    Assert-LastNativeCommand -Action $Action
    return ($result -join "`n")
}

function Get-HaCoreRuntimeContext {
    if ([string]::IsNullOrWhiteSpace($env:HA_TOKEN)) {
        throw "HA_TOKEN is required for an approved deployment runtime check. Set HA_TOKEN or SCENE_STUDIO_HA_TOKEN in the environment."
    }
    try {
        $config = Invoke-RestMethod -Uri "$HaBase/api/config" -Method Get -Headers @{ Authorization = "Bearer $env:HA_TOKEN" } -TimeoutSec 15
    } catch {
        throw "Could not read Home Assistant Core runtime context from $HaBase/api/config: $($_.Exception.Message)"
    }
    if ([string]::IsNullOrWhiteSpace([string]$config.version)) {
        throw 'Home Assistant Core runtime context did not include a version.'
    }
    Write-Host "Home Assistant Core version: $($config.version)"
}

function Get-SourceFiles {
    if (-not (Test-Path -LiteralPath $SourceRoot -PathType Container)) {
        throw "Scene Studio Python package is missing: $SourceRoot"
    }
    $prefix = $SourceRoot.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    $files = @(Get-ChildItem -LiteralPath $SourceRoot -Recurse -File -Filter '*.py' | Sort-Object FullName)
    if ($files.Count -eq 0) {
        throw "Scene Studio Python package contains no .py files: $SourceRoot"
    }
    $records = foreach ($file in $files) {
        $relative = $file.FullName.Substring($prefix.Length).Replace('\', '/')
        if ($relative -match '(^|/)\.\.(/|$)') {
            throw "Unsafe source path: $relative"
        }
        [PSCustomObject]@{
            LocalPath = $file.FullName
            Relative = $relative
            Sha256 = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    }
    foreach ($required in @('__init__.py', 'appdaemon_adapter/adapter.py')) {
        if (-not ($records.Relative -contains $required)) {
            throw "Required Scene Studio source file is missing: $required"
        }
    }
    return @($records)
}

function Push-HaFile {
    param(
        [Parameter(Mandatory = $true)][string]$LocalPath,
        [Parameter(Mandatory = $true)][string]$RemotePath
    )
    $remoteDirectory = ($RemotePath -replace '/[^/]+$', '')
    $quotedDirectory = ConvertTo-BashSingleQuoted $remoteDirectory
    Invoke-HaSsh -Command "sudo install -d -m 755 '$quotedDirectory'" -Action "Creating remote directory $remoteDirectory" | Out-Null
    $encoded = [Convert]::ToBase64String([System.IO.File]::ReadAllBytes($LocalPath))
    $quotedRemotePath = ConvertTo-BashSingleQuoted $RemotePath
    $encoded | & ssh @script:SshArgs $HaHost "base64 -d | sudo tee '$quotedRemotePath' > /dev/null"
    Assert-LastNativeCommand -Action "Uploading $RemotePath"
}

function Get-RemoteSha256 {
    param([Parameter(Mandatory = $true)][string]$RemotePath)
    $quotedPath = ConvertTo-BashSingleQuoted $RemotePath
    return (Invoke-HaSsh -Command "sha256sum '$quotedPath' | cut -d ' ' -f1" -Action "Hashing remote source $RemotePath").Trim().ToLowerInvariant()
}

function Assert-RemotePackageMatchesLocal {
    param(
        [Parameter(Mandatory = $true)][object[]]$Files,
        [Parameter(Mandatory = $true)][string]$RemoteDirectory
    )
    $quotedDirectory = ConvertTo-BashSingleQuoted $RemoteDirectory
    $remoteListText = Invoke-HaSsh -Command "cd '$quotedDirectory' && find . -type f -name '*.py' | sed 's#^./##' | sort" -Action 'Listing deployed Scene Studio sources'
    $remoteFiles = @($remoteListText -split "`r?`n" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    $localFiles = @($Files | ForEach-Object { $_.Relative } | Sort-Object)
    $difference = Compare-Object -ReferenceObject $localFiles -DifferenceObject $remoteFiles
    if ($difference) {
        $rendered = $difference | ForEach-Object { "$($_.SideIndicator) $($_.InputObject)" }
        throw "Deployed Scene Studio source set differs from local source: $($rendered -join '; ')"
    }
    foreach ($file in $Files) {
        $actual = Get-RemoteSha256 -RemotePath "$RemoteDirectory/$($file.Relative)"
        if ($actual -ne $file.Sha256) {
            throw "Hash mismatch for Scene Studio source '$($file.Relative)': local $($file.Sha256), remote $actual"
        }
    }
}

function Assert-PythonSyntax {
    param([Parameter(Mandatory = $true)][string]$RemoteDirectory)
    $quotedDirectory = ConvertTo-BashSingleQuoted $RemoteDirectory
    # No embedded double quotes anywhere in the remote command: Windows
    # PowerShell strips bare '"' while marshalling arguments to ssh.exe,
    # which silently corrupted this snippet once (encoding=utf-8). The glob
    # and encoding travel as argv instead; only single quotes remain, which
    # ConvertTo-BashSingleQuoted escapes correctly for the remote shell.
    $script = 'import ast,pathlib,sys; root=pathlib.Path(sys.argv[1]); [ast.parse(p.read_text(encoding=sys.argv[3]), filename=str(p)) for p in root.rglob(sys.argv[2])]'
    $quotedScript = ConvertTo-BashSingleQuoted $script
    Invoke-HaSsh -Command "python3 -c '$quotedScript' '$quotedDirectory' '*.py' utf-8" -Action 'Parsing staged Scene Studio Python sources' | Out-Null
}

function Get-SceneStudioStatus {
    $response = Invoke-HaSsh -Command "curl -fsS --max-time 10 -X POST -H 'Content-Type: application/json' --data '{`"method`":`"GET`",`"path`":`"/status`"}' http://127.0.0.1:5050/api/appdaemon/scene_studio_api" -Action 'Checking Scene Studio API status'
    try {
        return ($response | ConvertFrom-Json)
    } catch {
        throw "Scene Studio API returned invalid JSON: $response"
    }
}

function Get-SceneStudioRuntime {
    $status = Get-SceneStudioStatus
    if ($status.PSObject.Properties.Name -notcontains 'status' -or [int]$status.status -ne 200) {
        throw "Scene Studio API status check did not return HTTP 200: $($status | ConvertTo-Json -Compress -Depth 8)"
    }
    if ($status.PSObject.Properties.Name -notcontains 'body' -or $null -eq $status.body) {
        throw 'Scene Studio API status response has no body.'
    }
    $runtime = $status.body.runtime
    if ($null -eq $runtime -or [string]::IsNullOrWhiteSpace([string]$runtime.mode)) {
        throw 'Scene Studio API status response has no runtime.mode.'
    }
    $knownModes = @('normal', 'read_only', 'registry_admin', 'r2_validation', 'r5_validation')
    if ($knownModes -notcontains [string]$runtime.mode) {
        throw "Scene Studio API reported unknown runtime mode '$($runtime.mode)'."
    }
    if ($null -eq $runtime.allowed_commands) {
        throw 'Scene Studio API status response has no runtime.allowed_commands.'
    }
    return $runtime
}

function Test-SceneStudioService {
    param(
        [string]$ExpectedMode = ''
    )
    $runtime = Get-SceneStudioRuntime
    if (-not [string]::IsNullOrWhiteSpace($ExpectedMode) -and [string]$runtime.mode -ne $ExpectedMode) {
        throw "Scene Studio runtime mode changed during package deploy: expected '$ExpectedMode', got '$($runtime.mode)'."
    }
    return $runtime
}

function Restart-AppDaemonAddon {
    param(
        [string]$ExpectedMode = '',
        # Fresh-install rollback semantics: after quarantining the new tree,
        # the Scene Studio API being ABSENT again is SUCCESS, not failure.
        [switch]$AllowAbsent
    )
    Write-Host "Restarting AppDaemon add-on $AddonSlug..."
    try {
        Invoke-RestMethod -Uri "$HaBase/api/services/hassio/addon_restart" -Method Post -Headers @{ Authorization = "Bearer $env:HA_TOKEN" } -ContentType 'application/json' -Body (@{ addon = $AddonSlug } | ConvertTo-Json -Compress) -TimeoutSec 20 | Out-Null
    } catch {
        throw "Could not restart AppDaemon add-on $AddonSlug through the authenticated HA API: $($_.Exception.Message)"
    }
    $lastError = $null
    foreach ($attempt in 1..30) {
        Start-Sleep -Seconds 2
        try {
            $runtime = Test-SceneStudioService -ExpectedMode $ExpectedMode
            Write-Host "Scene Studio API healthy after $attempt restart check(s) (mode=$($runtime.mode))."
            return
        } catch {
            $lastError = $_
            if ($AllowAbsent) {
                # Proven-absent probe: the endpoint failing to answer IS the
                # pre-install state (a half-installed tree would still answer
                # or the add-on would error, neither of which is 'absent').
                $rawProbe = "curl -fsS --max-time 5 -X POST -H 'Content-Type: application/json' --data '{`"method`":`"GET`",`"path`":`"/status`"}' http://127.0.0.1:5050/api/appdaemon/scene_studio_api > /dev/null 2>&1 && echo scene_studio=present || echo scene_studio=absent"
                $probe = Invoke-HaSsh -Command $rawProbe -Action 'Probing Scene Studio API absence after rollback'
                if ($probe -match 'scene_studio=absent') {
                    Write-Host "Scene Studio API is absent again after $attempt restart check(s) - pre-install state restored."
                    return
                }
            }
        }
    }
    throw "Scene Studio API did not become healthy within 60 seconds after AppDaemon restart: $($lastError.Exception.Message)"
}

if (-not $Apply -and -not $VerifyOnly) {
    $sourceFiles = Get-SourceFiles
    Write-Host "Dry run complete. Local Scene Studio package verified: $($sourceFiles.Count) Python files. No HA connection or write was made."
    exit 0
}

$sourceFiles = Get-SourceFiles
Write-Host "Local Scene Studio package verified: $($sourceFiles.Count) Python files."
Get-HaCoreRuntimeContext
$quotedTarget = ConvertTo-BashSingleQuoted $RemoteTarget
$quotedRoot = ConvertTo-BashSingleQuoted $RemoteRoot
$quotedApps = ConvertTo-BashSingleQuoted "$RemoteRoot/apps"

# Proven remote state (never inferred from a caught failure): classify the
# deployment as a FIRST INSTALL (target absent) or an UPGRADE (target
# present) before touching anything. The upgrade path below is byte-for-byte
# the historical behavior; the fresh path has its own safety gates.
$stateLine = Invoke-HaSsh -Command "if test -d '$quotedTarget'; then echo backend=present; else echo backend=absent; fi" -Action 'Classifying Scene Studio backend deployment state'
$isFresh = ($stateLine -match 'backend=absent')
Write-Host ("Remote state: Scene Studio backend {0} at HA:{1} ({2})." -f $(if ($isFresh) { 'ABSENT' } else { 'present' }), $RemoteTarget, $(if ($isFresh) { 'fresh install' } else { 'upgrade' }))
if ($FreshInstall -and -not $isFresh) {
    throw (
        "-FreshInstall was requested, but HA:$RemoteTarget already exists. Use the upgrade path " +
        '(omit -FreshInstall), or remove the existing install deliberately first.'
    )
}

if ($isFresh) {
    if (-not $Apply) {
        # -VerifyOnly (or a dry run) against an absent target: report the
        # truthful state instead of installing anything.
        throw "Scene Studio backend is not installed at HA:$RemoteTarget - there is nothing to verify. Use -Apply -FreshInstall to install."
    }
    # ---- fresh install ------------------------------------------------
    # Ownership derives from the EXISTING AppDaemon parent directories (the
    # apps/ directory when present, else the add-on config root). There is
    # no previous package, no backup to take, and no runtime mode to
    # preserve: the first install must come up in the profile-requested
    # safe mode 'registry_admin'.
    $ownerLine = Invoke-HaSsh -Command "if test -d '$quotedApps'; then stat -c '%u:%g' '$quotedApps'; else stat -c '%u:%g' '$quotedRoot'; fi" -Action 'Deriving fresh-install ownership from the existing AppDaemon directories'
    # [string] cast: @() would leave an Object[] that fails [string] parameter
# binding in ConvertTo-BashSingleQuoted on some pwsh builds.
$remoteOwner = [string]($ownerLine -split "`r?`n" | Where-Object { $_ -match '^\d+:\d+$' } | Select-Object -Last 1)
    if ([string]::IsNullOrWhiteSpace($remoteOwner)) {
        throw 'Could not derive an owner for the fresh install from the AppDaemon directories.'
    }
    Invoke-HaSsh -Command "test ! -e '$quotedTarget'" -Action 'Confirming Scene Studio backend is still absent before install'

    $quotedStage = ConvertTo-BashSingleQuoted $RemoteStage
    Invoke-HaSsh -Command "set -eu; test ! -e '$quotedStage'; sudo install -d -m 755 '$quotedStage'" -Action 'Creating Scene Studio backend staging directory' | Out-Null
    $activationStarted = $false
    try {
        foreach ($file in $sourceFiles) {
            Push-HaFile -LocalPath $file.LocalPath -RemotePath "$RemoteStage/$($file.Relative)"
        }
        Assert-RemotePackageMatchesLocal -Files $sourceFiles -RemoteDirectory $RemoteStage
        Assert-PythonSyntax -RemoteDirectory $RemoteStage

        $quotedOwner = ConvertTo-BashSingleQuoted $remoteOwner
        $quotedFailed = ConvertTo-BashSingleQuoted $RemoteFailed
        Write-Host 'Activating fresh Scene Studio backend...'
        $activationStarted = $true
        Invoke-HaSsh -Command "set -eu; test -d '$quotedStage'; test -f '$quotedStage/appdaemon_adapter/adapter.py'; test ! -e '$quotedTarget'; sudo mv '$quotedStage' '$quotedTarget'; sudo chown -R '$quotedOwner' '$quotedTarget'" -Action 'Activating fresh Scene Studio backend' | Out-Null
        Restart-AppDaemonAddon -ExpectedMode 'registry_admin'
        Assert-RemotePackageMatchesLocal -Files $sourceFiles -RemoteDirectory $RemoteTarget
    } catch {
        $deploymentError = $_.Exception.Message
        if ($activationStarted) {
            # Rollback for a failed FIRST install = return to the pre-install
            # absent state: quarantine the newly installed tree (kept for
            # diagnosis) and restart. The API must then be ABSENT again.
            try {
                Invoke-HaSsh -Command "if test -d '$quotedTarget' && test ! -e '$quotedFailed'; then sudo mv '$quotedTarget' '$quotedFailed'; fi" -Action 'Quarantining the failed first-install Scene Studio backend' | Out-Null
                Restart-AppDaemonAddon -AllowAbsent
            } catch {
                throw "Scene Studio fresh install failed and quarantine also failed. Newly installed tree: HA:$RemoteTarget. Deployment error: $deploymentError. Quarantine error: $($_.Exception.Message)"
            }
            throw "Scene Studio fresh install failed; the new tree was quarantined at HA:${RemoteFailed} and the pre-install (absent) state was restored. Error: $deploymentError"
        }
        try {
            Invoke-HaSsh -Command "test ! -d '$quotedStage' || sudo rm -rf '$quotedStage'" -Action 'Cleaning failed Scene Studio backend staging directory' | Out-Null
        } catch {
            Write-Warning "Could not clean staging directory ${RemoteStage}: $_"
        }
        throw "Scene Studio fresh install failed before activation; nothing was changed on HA. Error: $deploymentError"
    }
    Write-Host 'Fresh install passed. The runtime must now be registry_admin (provider writes blocked).'
    Write-Host 'First-run: open the Workbench Setup view to run discovery and adopt fixtures.'
    exit 0
}

$runtimeInfo = Invoke-HaSsh -Command "set -eu; test -d '$quotedTarget'; python3 --version; stat -c '%u:%g' '$quotedTarget'" -Action 'Reading AppDaemon backend runtime context'
$runtimeLines = @($runtimeInfo -split "`r?`n" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
$remoteOwner = $runtimeLines | Where-Object { $_ -match '^\d+:\d+$' } | Select-Object -Last 1
if ([string]::IsNullOrWhiteSpace($remoteOwner)) {
    throw 'Could not determine the current Scene Studio package owner from HA runtime context.'
}

if ($VerifyOnly) {
    if ($isFresh) {
        throw "Scene Studio backend is not installed at HA:$RemoteTarget - there is nothing to verify. Use the fresh-install path (-Apply -FreshInstall)."
    }
    Assert-RemotePackageMatchesLocal -Files $sourceFiles -RemoteDirectory $RemoteTarget
    $runtime = Test-SceneStudioService
    Write-Host "Verification passed: HA is running the exact local Scene Studio Python package and the Scene Studio API is healthy (mode=$($runtime.mode))."
    exit 0
}

$quotedRoot = ConvertTo-BashSingleQuoted $RemoteRoot
$quotedBackupDir = ConvertTo-BashSingleQuoted $RemoteBackupDir
$quotedArchive = ConvertTo-BashSingleQuoted $RemoteArchive
Write-Host "Creating HA backend backup at $RemoteArchive ..."
Invoke-HaSsh -Command "set -eu; test ! -e '$quotedBackupDir'; sudo install -d -m 750 '$quotedBackupDir'; cd '$quotedRoot'; sudo tar czf '$quotedArchive' apps/scene_studio; sudo tar tzf '$quotedArchive' | grep -Fx 'apps/scene_studio/appdaemon_adapter/adapter.py' > /dev/null; sudo sha256sum '$quotedArchive'" -Action 'Creating Scene Studio backend pre-deploy backup' | Write-Host

$quotedStage = ConvertTo-BashSingleQuoted $RemoteStage
Invoke-HaSsh -Command "set -eu; test ! -e '$quotedStage'; sudo install -d -m 755 '$quotedStage'" -Action 'Creating Scene Studio backend staging directory' | Out-Null
$activationStarted = $false
$preDeployMode = $null
try {
    foreach ($file in $sourceFiles) {
        Push-HaFile -LocalPath $file.LocalPath -RemotePath "$RemoteStage/$($file.Relative)"
    }
    Assert-RemotePackageMatchesLocal -Files $sourceFiles -RemoteDirectory $RemoteStage
    Assert-PythonSyntax -RemoteDirectory $RemoteStage

    $preDeployRuntime = Get-SceneStudioRuntime
    $preDeployMode = [string]$preDeployRuntime.mode
    Write-Host "Pre-deploy runtime mode: $preDeployMode (package deploy must preserve this mode)"

    $quotedPrevious = ConvertTo-BashSingleQuoted $RemotePrevious
    $quotedFailed = ConvertTo-BashSingleQuoted $RemoteFailed
    $quotedOwner = ConvertTo-BashSingleQuoted $remoteOwner
    Write-Host 'Activating staged Scene Studio backend...'
    $activationStarted = $true
    Invoke-HaSsh -Command "set -eu; test -d '$quotedStage'; test -f '$quotedStage/appdaemon_adapter/adapter.py'; test ! -e '$quotedPrevious'; sudo mv '$quotedTarget' '$quotedPrevious'; sudo mv '$quotedStage' '$quotedTarget'; sudo chown -R '$quotedOwner' '$quotedTarget'" -Action 'Activating staged Scene Studio backend' | Out-Null
    Restart-AppDaemonAddon -ExpectedMode $preDeployMode
    Assert-RemotePackageMatchesLocal -Files $sourceFiles -RemoteDirectory $RemoteTarget
} catch {
    $deploymentError = $_.Exception.Message
    if ($activationStarted) {
        try {
            $quotedPrevious = ConvertTo-BashSingleQuoted $RemotePrevious
            $quotedFailed = ConvertTo-BashSingleQuoted $RemoteFailed
            $quotedOwner = ConvertTo-BashSingleQuoted $remoteOwner
            Invoke-HaSsh -Command "if test -d '$quotedPrevious' && test -d '$quotedTarget' && test ! -e '$quotedFailed'; then sudo mv '$quotedTarget' '$quotedFailed'; sudo mv '$quotedPrevious' '$quotedTarget'; sudo chown -R '$quotedOwner' '$quotedTarget'; fi" -Action 'Rolling back interrupted Scene Studio backend activation' | Out-Null
            Restart-AppDaemonAddon -ExpectedMode $preDeployMode
        } catch {
            throw "Scene Studio backend deployment failed and rollback also failed. Previous package: HA:${RemotePrevious}. Deployment error: $deploymentError. Rollback error: $($_.Exception.Message)"
        }
    } else {
        try {
            Invoke-HaSsh -Command "test ! -d '$quotedStage' || sudo rm -rf '$quotedStage'" -Action 'Cleaning failed Scene Studio backend staging directory' | Out-Null
        } catch {
            Write-Warning "Could not clean staging directory ${RemoteStage}: $_"
        }
    }
    throw "Scene Studio backend deployment failed; the previous backend was restored when activation had begun. Previous package: HA:${RemotePrevious}. Error: $deploymentError"
}

Write-Host "Deployment passed. Backup archive: HA:${RemoteArchive}"
Write-Host "Previous package retained for rollback: HA:${RemotePrevious}"
Write-Host 'Only the AppDaemon add-on was restarted; Home Assistant Core was not changed or restarted.'
