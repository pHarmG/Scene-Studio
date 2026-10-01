<#
.SYNOPSIS
  Builds, backs up, deploys, and verifies the Scene Studio Workbench on HA.

.DESCRIPTION
  The Workbench is the static SPA served by the AppDaemon add-on at
  /local/scene_studio/. This script deploys only its built dist/ files. It
  does not modify the Scene Studio Python backend, AppDaemon YAML, stores,
  dashboards, or Home Assistant Core.

  Run under PowerShell 7 (pwsh) ONLY - see the version guard below.
  Live writes are intentionally opt-in: -Apply is required. Per AGENTS.md,
  an agent may use -Apply only after the user has explicitly approved this
  HA deployment.

  Each deployment saves a compressed pre-deploy archive and preserves the
  previous live directory alongside it, allowing a straightforward rollback.
#>

[CmdletBinding()]
param(
    [switch]$Apply,
    [switch]$VerifyOnly,
    [switch]$SkipBuild,
    [switch]$SkipSmoke,
    [switch]$FreshInstall,
    [switch]$Prebuilt,
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
# private default): explicit -HaUrl, then SCENE_STUDIO_HA_URL (set by the
# portable installer from the deployment profile), then the gitignored local
# operator profile (.scene-studio.local.json, key "ha_api_url") / pre-existing
# HA_URL, else FAIL. A clean checkout must never guess a private HA address.
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
$WorkbenchRoot = Join-Path $RepoRoot 'workbench'
$DistRoot = Join-Path $WorkbenchRoot 'dist'
$RemoteRoot = $AddonConfigRoot.TrimEnd('/')
$RemoteWwwRoot = "$RemoteRoot/www"
$RemoteTarget = "$RemoteWwwRoot/scene_studio"
$AddonSlug = Split-Path $RemoteRoot -Leaf
$Stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')
$RemoteStage = "/tmp/scene-studio-workbench-$Stamp"
$RemoteBackupDir = "$RemoteRoot/backups/scene-studio-workbench-$Stamp"
$RemoteArchive = "$RemoteBackupDir/www-scene_studio-before.tar.gz"
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
    $embeddedQuote = $singleQuote + $doubleQuote + $singleQuote + $doubleQuote + $singleQuote
    # Callers surround this result with shell single quotes.  Keep the quotes
    # out of this helper so paths are quoted exactly once.
    return $Value.Replace($singleQuote, $embeddedQuote)
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

function Invoke-WorkbenchNpmScript {
    param([Parameter(Mandatory = $true)][string]$Name)

    Push-Location $WorkbenchRoot
    try {
        Write-Host "Running npm run $Name..."
        & npm run $Name
        Assert-LastNativeCommand -Action "npm run $Name"
    } finally {
        Pop-Location
    }
}

function Get-HaCoreRuntimeContext {
    # The `ha` CLI in the SSH add-on shell does not inherit Supervisor
    # credentials, so `ha core info` returns 401 there. The local HA REST
    # credential is the supported, authenticated source for the Core version.
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

function Get-RelativeDistFiles {
    if (-not (Test-Path -LiteralPath $DistRoot -PathType Container)) {
        throw "Workbench build output is missing: $DistRoot"
    }

    $prefix = $DistRoot.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    $files = @(Get-ChildItem -LiteralPath $DistRoot -Recurse -File | Sort-Object FullName)
    if ($files.Count -eq 0) {
        throw "Workbench build output contains no files: $DistRoot"
    }

    $records = foreach ($file in $files) {
        $relative = $file.FullName.Substring($prefix.Length).Replace('\', '/')
        if ($relative -match '(^|/)\.\.(/|$)') {
            throw "Unsafe build output path: $relative"
        }
        [PSCustomObject]@{
            LocalPath = $file.FullName
            Relative  = $relative
            Sha256    = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    }

    if (-not ($records.Relative -contains 'index.html')) {
        throw "Workbench build output has no index.html: $DistRoot"
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
    return (Invoke-HaSsh -Command "sha256sum '$quotedPath' | cut -d ' ' -f1" -Action "Hashing remote file $RemotePath").Trim().ToLowerInvariant()
}

function Assert-RemoteTreeMatchesLocal {
    param(
        [Parameter(Mandatory = $true)][object[]]$Files,
        [Parameter(Mandatory = $true)][string]$RemoteDirectory
    )

    $quotedDirectory = ConvertTo-BashSingleQuoted $RemoteDirectory
    $remoteListText = Invoke-HaSsh -Command "cd '$quotedDirectory' && find . -type f | sed 's#^./##' | sort" -Action "Listing deployed Workbench files"
    $remoteFiles = @($remoteListText -split "`r?`n" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    $localFiles = @($Files | ForEach-Object { $_.Relative } | Sort-Object)
    $difference = Compare-Object -ReferenceObject $localFiles -DifferenceObject $remoteFiles
    if ($difference) {
        $rendered = $difference | ForEach-Object { "$($_.SideIndicator) $($_.InputObject)" }
        throw "Deployed Workbench file set differs from dist/: $($rendered -join '; ')"
    }

    foreach ($file in $Files) {
        $actual = Get-RemoteSha256 -RemotePath "$RemoteDirectory/$($file.Relative)"
        if ($actual -ne $file.Sha256) {
            throw "Hash mismatch for deployed asset '$($file.Relative)': local $($file.Sha256), remote $actual"
        }
    }
}

function Test-WorkbenchService {
    param([Parameter(Mandatory = $true)][string]$ExpectedIndexSha256)

    $quotedIndex = ConvertTo-BashSingleQuoted "$RemoteTarget/index.html"
    $served = Invoke-HaSsh -Command "set -eu; served_hash=`$(curl -fsS --max-time 10 http://127.0.0.1:5050/local/scene_studio/index.html | sha256sum | cut -d ' ' -f1); test `"`$served_hash`" = `"$ExpectedIndexSha256`"; curl -fsS --max-time 10 http://127.0.0.1:5050/local/scene_studio/index.html | grep -F 'Scene Studio Workbench' > /dev/null; test -f '$quotedIndex'" -Action 'Checking AppDaemon-served Workbench page'
    $null = $served

    $statusJson = Invoke-HaSsh -Command "curl -fsS --max-time 10 -X POST -H 'Content-Type: application/json' --data '{`"method`":`"GET`",`"path`":`"/status`"}' http://127.0.0.1:5050/api/appdaemon/scene_studio_api" -Action 'Checking Scene Studio API status'
    try {
        $status = $statusJson | ConvertFrom-Json
    } catch {
        throw "Scene Studio API returned invalid JSON: $statusJson"
    }
    if ($status.PSObject.Properties.Name -notcontains 'status' -or [int]$status.status -ne 200) {
        throw "Scene Studio API status check did not return HTTP 200: $statusJson"
    }
    if ($status.PSObject.Properties.Name -notcontains 'body') {
        throw "Scene Studio API status response has no body: $statusJson"
    }
}

if (-not (Test-Path -LiteralPath $WorkbenchRoot -PathType Container)) {
    throw "Scene Studio Workbench package not found: $WorkbenchRoot"
}

if ($Prebuilt) {
    # Explicit PREBUILT distribution path (portable bundle): the bundle
    # carries only the production dist/ — no package.json, no Node sources —
    # so npm build/smoke are structurally unavailable, not skipped. dist
    # integrity is still verified below (Get-RelativeDistFiles: non-empty,
    # index.html present, per-file hashes) and the remote gates are
    # identical. Source-repo deployments keep the build+smoke gates.
    if (Test-Path -LiteralPath (Join-Path $WorkbenchRoot 'package.json')) {
        Write-Warning '-Prebuilt was given in a source checkout (package.json present); build/smoke stay SKIPPED for this run — prefer the default gates for repo deployments.'
    } else {
        Write-Host 'Prebuilt bundle path: no Node sources in this tree (expected for a distribution bundle).'
    }
} else {
    if (-not $SkipBuild) {
        Invoke-WorkbenchNpmScript -Name 'build'
    } else {
        Write-Host 'Skipping Workbench build (-SkipBuild).'
    }

    if (-not $SkipSmoke) {
        Invoke-WorkbenchNpmScript -Name 'smoke'
    } else {
        Write-Warning 'Skipping Workbench smoke test (-SkipSmoke).'
    }
}

$distFiles = Get-RelativeDistFiles
$indexFile = $distFiles | Where-Object { $_.Relative -eq 'index.html' } | Select-Object -First 1
Write-Host "Local Workbench build verified: $($distFiles.Count) files; index SHA-256 $($indexFile.Sha256)"

if (-not $Apply -and -not $VerifyOnly) {
    Write-Host 'Dry run complete. No HA connection or write was made. Use -Apply only after explicit user approval, or -VerifyOnly to check the current live deployment.'
    exit 0
}

$quotedRoot = ConvertTo-BashSingleQuoted $RemoteRoot
$quotedWwwRoot = ConvertTo-BashSingleQuoted $RemoteWwwRoot
$quotedTarget = ConvertTo-BashSingleQuoted $RemoteTarget
Get-HaCoreRuntimeContext

# Proven remote state (never inferred from a caught failure): classify the
# deployment as a FIRST INSTALL (www/scene_studio absent) or an UPGRADE.
$stateLine = Invoke-HaSsh -Command "if test -d '$quotedTarget'; then echo workbench=present; else echo workbench=absent; fi" -Action 'Classifying Scene Studio Workbench deployment state'
$isFresh = ($stateLine -match 'workbench=absent')
Write-Host ("Remote state: Workbench {0} at HA:{1} ({2})." -f $(if ($isFresh) { 'ABSENT' } else { 'present' }), $RemoteTarget, $(if ($isFresh) { 'fresh install' } else { 'upgrade' }))
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
        throw "Scene Studio Workbench is not installed at HA:$RemoteTarget - there is nothing to verify. Use -Apply -FreshInstall to install."
    }
    # ---- fresh install (static assets; no restart required) -----------
    # Ownership derives from the EXISTING parent directories (www/ when
    # present, else the add-on config root). There is no previous tree, no
    # backup to take, and rollback means removing/quarantining the new tree.
    $ownerLine = Invoke-HaSsh -Command "if test -d '$quotedWwwRoot'; then stat -c '%u:%g' '$quotedWwwRoot'; else stat -c '%u:%g' '$quotedRoot'; fi" -Action 'Deriving fresh-install ownership from the existing AppDaemon directories'
    # [string] cast: @() would leave an Object[] that fails [string] parameter
# binding in ConvertTo-BashSingleQuoted on some pwsh builds.
$remoteOwner = [string]($ownerLine -split "`r?`n" | Where-Object { $_ -match '^\d+:\d+$' } | Select-Object -Last 1)
    if ([string]::IsNullOrWhiteSpace($remoteOwner)) {
        throw 'Could not derive an owner for the fresh install from the AppDaemon directories.'
    }
    Invoke-HaSsh -Command "test ! -e '$quotedTarget'" -Action 'Confirming Workbench is still absent before install'

    Write-Host "Staging $($distFiles.Count) Workbench files on HA..."
    $quotedStage = ConvertTo-BashSingleQuoted $RemoteStage
    Invoke-HaSsh -Command "set -eu; test ! -e '$quotedStage'; sudo install -d -m 755 '$quotedStage'" -Action 'Creating Workbench staging directory' | Out-Null
    $activationStarted = $false
    try {
        foreach ($file in $distFiles) {
            Push-HaFile -LocalPath $file.LocalPath -RemotePath "$RemoteStage/$($file.Relative)"
        }
        Assert-RemoteTreeMatchesLocal -Files $distFiles -RemoteDirectory $RemoteStage

        $quotedOwner = ConvertTo-BashSingleQuoted $remoteOwner
        $quotedFailed = ConvertTo-BashSingleQuoted $RemoteFailed
        Write-Host 'Activating fresh Workbench build...'
        $activationStarted = $true
        Invoke-HaSsh -Command "set -eu; test -d '$quotedStage'; test -f '$quotedStage/index.html'; test ! -e '$quotedTarget'; sudo mv '$quotedStage' '$quotedTarget'; sudo chown -R '$quotedOwner' '$quotedTarget'" -Action 'Activating fresh Workbench build' | Out-Null
        Assert-RemoteTreeMatchesLocal -Files $distFiles -RemoteDirectory $RemoteTarget
        Test-WorkbenchService -ExpectedIndexSha256 $indexFile.Sha256
    } catch {
        $verificationError = $_.Exception.Message
        if ($activationStarted) {
            # Failed verification of a FIRST install = return to the absent
            # pre-install state: quarantine the new tree (kept for diagnosis).
            try {
                Invoke-HaSsh -Command "if test -d '$quotedTarget' && test ! -e '$quotedFailed'; then sudo mv '$quotedTarget' '$quotedFailed'; fi" -Action 'Quarantining the failed first-install Workbench tree' | Out-Null
            } catch {
                throw "Workbench fresh install failed and quarantine also failed. Newly installed tree: HA:$RemoteTarget. Error: $verificationError. Quarantine error: $($_.Exception.Message)"
            }
            throw "Workbench fresh install failed verification; the new tree was quarantined at HA:${RemoteFailed} and the pre-install (absent) state was restored. Error: $verificationError"
        }
        try {
            Invoke-HaSsh -Command "test ! -d '$quotedStage' || sudo rm -rf '$quotedStage'" -Action 'Cleaning failed Workbench staging directory' | Out-Null
        } catch {
            Write-Warning "Could not clean staging directory ${RemoteStage}: $_"
        }
        throw "Workbench fresh install failed before activation; nothing was changed on HA. Error: $verificationError"
    }
    Write-Host 'Fresh Workbench install passed and the served page/API health checks are green.'
    exit 0
}

$runtimeInfo = Invoke-HaSsh -Command "set -eu; echo host=`$(hostname); uname -srmo; test -d '$quotedRoot'; test -d '$quotedWwwRoot'; test -d '$quotedTarget'; stat -c '%u:%g' '$quotedTarget'" -Action 'Reading AppDaemon Workbench runtime context'
$runtimeLines = @($runtimeInfo -split "`r?`n" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
$remoteOwner = $runtimeLines | Where-Object { $_ -match '^\d+:\d+$' } | Select-Object -Last 1
if ([string]::IsNullOrWhiteSpace($remoteOwner)) {
    throw "Could not determine the current Workbench directory owner from HA runtime context."
}

if ($VerifyOnly) {
    if ($isFresh) {
        throw "Scene Studio Workbench is not installed at HA:$RemoteTarget - there is nothing to verify. Use the fresh-install path (-Apply -FreshInstall -Prebuilt for a bundle)."
    }
    Assert-RemoteTreeMatchesLocal -Files $distFiles -RemoteDirectory $RemoteTarget
    Test-WorkbenchService -ExpectedIndexSha256 $indexFile.Sha256
    Write-Host "Verification passed: HA is serving the exact local Workbench build at /local/scene_studio/ and the Scene Studio API is healthy."
    exit 0
}

Write-Host "Creating HA backup at $RemoteArchive ..."
$quotedBackupDir = ConvertTo-BashSingleQuoted $RemoteBackupDir
$quotedArchive = ConvertTo-BashSingleQuoted $RemoteArchive
Invoke-HaSsh -Command "set -eu; test ! -e '$quotedBackupDir'; sudo install -d -m 750 '$quotedBackupDir'; cd '$quotedRoot'; sudo tar czf '$quotedArchive' www/scene_studio; sudo tar tzf '$quotedArchive' | grep -Fx 'www/scene_studio/index.html' > /dev/null; sudo sha256sum '$quotedArchive'" -Action 'Creating Workbench pre-deploy backup' | Write-Host

Write-Host "Staging $($distFiles.Count) Workbench files on HA..."
$quotedStage = ConvertTo-BashSingleQuoted $RemoteStage
Invoke-HaSsh -Command "set -eu; test ! -e '$quotedStage'; sudo install -d -m 755 '$quotedStage'" -Action 'Creating Workbench staging directory' | Out-Null
$activationStarted = $false
try {
    foreach ($file in $distFiles) {
        Push-HaFile -LocalPath $file.LocalPath -RemotePath "$RemoteStage/$($file.Relative)"
    }
    Assert-RemoteTreeMatchesLocal -Files $distFiles -RemoteDirectory $RemoteStage

    $quotedPrevious = ConvertTo-BashSingleQuoted $RemotePrevious
    $quotedOwner = ConvertTo-BashSingleQuoted $remoteOwner
    $quotedFailed = ConvertTo-BashSingleQuoted $RemoteFailed
    Write-Host 'Activating staged Workbench build...'
    $activationStarted = $true
    Invoke-HaSsh -Command "set -eu; test -d '$quotedStage'; test -f '$quotedStage/index.html'; test ! -e '$quotedPrevious'; sudo mv '$quotedTarget' '$quotedPrevious'; sudo mv '$quotedStage' '$quotedTarget'; sudo chown -R '$quotedOwner' '$quotedTarget'" -Action 'Activating staged Workbench build' | Out-Null
} catch {
    # The saved live directory and compressed archive remain on HA for manual rollback.
    # Remove only the uniquely named, incomplete staging directory when it still exists.
    if ($activationStarted) {
        try {
            Invoke-HaSsh -Command "if test -d '$quotedPrevious' && test -d '$quotedTarget' && test ! -e '$quotedFailed'; then sudo mv '$quotedTarget' '$quotedFailed'; sudo mv '$quotedPrevious' '$quotedTarget'; sudo chown -R '$quotedOwner' '$quotedTarget'; fi" -Action 'Rolling back interrupted Workbench activation' | Out-Null
        } catch {
            Write-Warning "Could not roll back an interrupted activation. Previous live directory: HA:${RemotePrevious}. Error: $_"
        }
    }
    try {
        Invoke-HaSsh -Command "test ! -d '$quotedStage' || sudo rm -rf '$quotedStage'" -Action 'Cleaning failed Workbench staging directory' | Out-Null
    } catch {
        Write-Warning "Could not clean staging directory ${RemoteStage}: $_"
    }
    throw
}

try {
    Assert-RemoteTreeMatchesLocal -Files $distFiles -RemoteDirectory $RemoteTarget
    Test-WorkbenchService -ExpectedIndexSha256 $indexFile.Sha256
} catch {
    # A deployment whose files cannot be proven or whose served page/API fails
    # its health gate is not left active. Keep the failed tree for diagnosis.
    $verificationError = $_.Exception.Message
    $quotedFailed = ConvertTo-BashSingleQuoted $RemoteFailed
    try {
        Write-Warning 'Deployment verification failed; restoring the previous live Workbench directory.'
        Invoke-HaSsh -Command "set -eu; test -d '$quotedPrevious'; test -d '$quotedTarget'; test ! -e '$quotedFailed'; sudo mv '$quotedTarget' '$quotedFailed'; sudo mv '$quotedPrevious' '$quotedTarget'; sudo chown -R '$quotedOwner' '$quotedTarget'" -Action 'Rolling back failed Workbench deployment' | Out-Null
    } catch {
        throw "Deployment verification failed and automatic rollback also failed. Previous live directory: HA:${RemotePrevious}. Verification error: $verificationError. Rollback error: $($_.Exception.Message)"
    }
    throw "Deployment verification failed; the previous Workbench directory was restored. Failed build retained at HA:${RemoteFailed}. Verification error: $verificationError"
}
Write-Host "Deployment passed. Backup archive: HA:${RemoteArchive}"
Write-Host "Previous live directory retained for rollback: HA:${RemotePrevious}"
Write-Host 'No AppDaemon or Home Assistant Core restart was needed because this deploy changes only static Workbench assets.'
