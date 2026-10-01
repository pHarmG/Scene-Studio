<#
.SYNOPSIS
  Scene Studio profile-driven deployer — verify the release tree (when a
  manifest is present), preflight the deployment, and install/upgrade through
  the component deployers. Driven by the guided wizard; power users and CI
  can run it directly with a hand-written profile.

.DESCRIPTION
  Runs from the release tree (MANIFEST.sha256 present, verified byte-exact)
  or straight from a development checkout (no manifest; the release
  integrity gate is skipped and the deployers' own safety gates apply).

  Ordering is deliberate — everything that can fail LOCALLY fails before any
  remote contact:

    1. release manifest verification, when present (byte-exact, no unlisted payload);
    2. profile validation + resolution into the canonical shape;
    3. remote state classification (read-only ssh): backend/Workbench
       present vs absent -> UPGRADE vs FRESH INSTALL, never guessed from a
       caught failure;
    4. provider preflight: enabled Hue/WLED/hyperHDR hosts must be
       TCP-reachable before -Apply (reads only, no provider writes); a Hue
       install without bridge_id warns (first-run adoption blocker);
    5. only then -Apply, passing -FreshInstall / -Prebuilt explicitly to
       the deployers.

  A FRESH install (absent backend or Workbench) requires the profile to
  request runtime_mode 'registry_admin' — the safe first-install mode where
  provider writes are blocked. normal/read_only profiles are upgrade-only.

  The installer NEVER edits apps.yaml or secrets.yaml: it prints the
  generated scene_studio block and the operator merges it deliberately.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Profile,
    [switch]$Apply,
    [switch]$SkipBackend,
    [switch]$SkipWorkbench,
    [string]$HaHost = '',
    [string]$AddonConfigRoot = '',
    [int]$SshPort = 0
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ($PSVersionTable.PSVersion.Major -lt 7) {
    throw (
        'PowerShell 7 (pwsh) is required. Run: ' +
        "pwsh -NoProfile -ExecutionPolicy Bypass -File $($MyInvocation.MyCommand.Path) ..."
    )
}

$BundleRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$ProfileTool = Join-Path $PSScriptRoot 'scene-studio-profile.py'
$ManifestTool = Join-Path $PSScriptRoot 'verify_release_manifest.ps1'
# nested Join-Path (never '\' inside a segment): the same join must also
# resolve when this script runs under pwsh on macOS/Linux.
$BackendDeployer = Join-Path (Join-Path $BundleRoot 'installer') 'deploy_scene_studio_backend.ps1'
$WorkbenchDeployer = Join-Path (Join-Path $BundleRoot 'installer') 'deploy_scene_studio_workbench.ps1'
$Manifest = Join-Path $BundleRoot 'MANIFEST.sha256'

foreach ($required in @($ProfileTool, $ManifestTool, $BackendDeployer, $WorkbenchDeployer)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Required installer file missing: $required - re-extract the full release."
    }
}
# The manifest gate applies to RELEASE trees (byte-exact friend installs).
# A dev checkout has no MANIFEST.sha256; the deployers' own backup/verify/
# rollback gates still apply to every write.
$HasManifest = Test-Path -LiteralPath $Manifest

function Test-TcpReachable {
    param(
        [Parameter(Mandatory = $true)][string]$TargetHost,
        [Parameter(Mandatory = $true)][int]$Port,
        [int]$TimeoutMs = 3000
    )
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

function Split-HostPort {
    # 'host' or 'host:port' -> @(host, port-or-null)
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value)
    if ($Value -match '^\[?([^\]]+)\]?:(\d+)$') {
        return @($Matches[1], [int]$Matches[2])
    }
    return @($Value, $null)
}

function ConvertTo-BashSingleQuoted {
    # Escape a value for embedding inside single quotes in a remote shell
    # command (same convention as the deployers).
    param([Parameter(Mandatory = $true)][string]$Value)
    $singleQuote = [string][char]39
    $doubleQuote = [string][char]34
    return $Value.Replace($singleQuote, $singleQuote + $doubleQuote + $singleQuote + $doubleQuote + $singleQuote)
}

Write-Host '== 1. Release integrity (local, before any remote contact) =='
if ($HasManifest) {
    & pwsh -NoProfile -ExecutionPolicy Bypass -File $ManifestTool -ReleaseRoot $BundleRoot
    if ($LASTEXITCODE -ne 0) {
        throw 'Release manifest verification FAILED: the tree was changed, is incomplete, or carries unlisted files. Do not install from it.'
    }
} else {
    Write-Host '  No MANIFEST.sha256 here (development checkout) - skipping the release integrity gate.'
}

Write-Host ''
Write-Host '== 2. Profile validation =='
& python $ProfileTool validate --profile $Profile
if ($LASTEXITCODE -ne 0) {
    throw 'Profile validation failed - fix the profile and re-run.'
}
$resolveJson = & python $ProfileTool resolve --profile $Profile
if ($LASTEXITCODE -ne 0) {
    throw 'Profile resolution failed.'
}
# Canonical resolved shape (scene_studio_profile.normalized): providers and
# integrations are NESTED; consumers read $Settings.providers.<name>.
$Settings = $resolveJson | ConvertFrom-Json

$HaUrl = $Settings.ha_url
if (-not $HaHost) {
    if ($Settings.ha_ssh_host) {
        $HaHost = $Settings.ha_ssh_host
    } else {
        $HaHost = 'HA'
        Write-Warning "profile has no ha_ssh_host; defaulting the ssh alias to 'HA' (override with -HaHost)."
    }
}
if (-not $AddonConfigRoot) {
    $AddonConfigRoot = $Settings.appdaemon_config_root
}
if ($AddonConfigRoot -match 'REPLACE_ME') {
    throw (
        "appdaemon_config_root is still a placeholder ($AddonConfigRoot). Set the real AppDaemon " +
        'add-on config directory (find the slug under /addon_configs) in the profile or pass -AddonConfigRoot.'
    )
}

Write-Host ''
Write-Host '== 3. Deployment targets =='
Write-Host "  HA URL:              $HaUrl"
Write-Host "  ssh host:            $HaHost"
Write-Host "  AppDaemon config:    $AddonConfigRoot"
Write-Host "  Scene Studio store:  $($Settings.store_root)"
Write-Host "  Profile runtime:     $($Settings.runtime_mode)"
$providers = $Settings.providers
$enabledProviders = @()
foreach ($name in @('hue', 'wled', 'ha_light')) {
    if ($providers.$name.enabled) { $enabledProviders += $name }
}
Write-Host "  Providers enabled:   $($enabledProviders -join ', ')"
if ($Settings.integrations.hyperhdr.enabled) { Write-Host '  hyperHDR contention: configured (optional integration)' }

$script:SshArgs = @()
if ($SshPort -gt 0) {
    $script:SshArgs = @('-p', [string]$SshPort)
}

Write-Host ''
Write-Host '== 4. Remote state classification (read-only) =='
# Prove the configured add-on root exists BEFORE classifying the install:
# a nonexistent/mistyped AppDaemonConfigRoot must fail read-only preflight
# with an actionable error instead of reading as a valid fresh install.
$quotedRoot = ConvertTo-BashSingleQuoted -Value $AddonConfigRoot
$rootOutput = & ssh @script:SshArgs $HaHost "if test -d '$quotedRoot'; then echo addon_root=present; else echo addon_root=absent; fi"
if ($LASTEXITCODE -ne 0) {
    throw "Could not reach '$HaHost' over ssh to classify the deployment. Fix ssh access and re-run."
}
if ($rootOutput -match 'addon_root=absent') {
    throw (
        "AppDaemon config root '$AddonConfigRoot' does not exist on '$HaHost'. The add-on slug is " +
        'probably mistyped: find the exact directory on the HA host under /addon_configs (e.g. ' +
        "'/addon_configs/a0d7b954_appdaemon') and fix appdaemon_config_root in the profile, or pass " +
        '-AddonConfigRoot. Nothing was written.'
    )
}
$quotedAppsTarget = ConvertTo-BashSingleQuoted -Value "$AddonConfigRoot/apps/scene_studio"
$quotedWwwTarget = ConvertTo-BashSingleQuoted -Value "$AddonConfigRoot/www/scene_studio"
$stateOutput = & ssh @script:SshArgs $HaHost "if test -d '$quotedAppsTarget'; then echo backend=present; else echo backend=absent; fi; if test -d '$quotedWwwTarget'; then echo workbench=present; else echo workbench=absent; fi"
if ($LASTEXITCODE -ne 0) {
    throw "Could not reach '$HaHost' over ssh to classify the deployment. Fix ssh access and re-run."
}
$backendPresent = ($stateOutput -match 'backend=present')
$workbenchPresent = ($stateOutput -match 'workbench=present')
$backendFresh = -not $backendPresent
$workbenchFresh = -not $workbenchPresent
Write-Host ("  Scene Studio backend: {0}" -f $(if ($backendPresent) { "installed (upgrade)" } else { "ABSENT (fresh install)" }))
Write-Host ("  Workbench:            {0}" -f $(if ($workbenchPresent) { "installed (upgrade)" } else { "ABSENT (fresh install)" }))
$anyFresh = $backendFresh -or $workbenchFresh
if ($anyFresh -and $Settings.runtime_mode -ne 'registry_admin') {
    throw (
        "First-install invocation: the profile must request runtime_mode 'registry_admin' " +
        "(found '$($Settings.runtime_mode)'). A first install comes up with provider writes " +
        'blocked; switch to normal/read_only deliberately AFTER the registry is built. ' +
        'normal/read_only profiles are valid for UPGRADES of an existing install only.'
    )
}

Write-Host ''
Write-Host '== 5. Provider preflight (reads only; no provider writes) =='
$preflightErrors = @()
$preflightWarnings = @()
$hue = $providers.hue
if ($hue.enabled) {
    $hostPort = Split-HostPort -Value $hue.host
    $huePort = if ($null -ne $hostPort[1]) { $hostPort[1] } else { 443 }
    if (Test-TcpReachable -TargetHost $hostPort[0] -Port $huePort) {
        Write-Host "  Hue bridge $($hue.host): reachable (tcp/$huePort)"
    } else {
        $preflightErrors += "Hue bridge $($hue.host) is not reachable on tcp/$huePort - discovery and execution will fail."
    }
    if (-not $hue.bridge_id) {
        $preflightWarnings += 'providers.hue.bridge_id is unset: hue discovery works, but first-run fixture.adopt of Hue lights needs the bridge hardware id (Hue app > Settings > My Hue bridge).'
    }
} else {
    Write-Host '  Hue: disabled in profile'
}
$wled = $providers.wled
if ($wled.enabled) {
    foreach ($candidate in $wled.hosts) {
        $hostPort = Split-HostPort -Value $candidate
        $wledPort = if ($null -ne $hostPort[1]) { $hostPort[1] } else { 80 }
        if (Test-TcpReachable -TargetHost $hostPort[0] -Port $wledPort) {
            Write-Host "  WLED $candidate : reachable (tcp/$wledPort)"
        } else {
            $preflightErrors += "WLED controller $candidate is not reachable on tcp/$wledPort - discovery will skip it."
        }
    }
} else {
    Write-Host '  WLED: disabled in profile'
}
if ($providers.ha_light.enabled) {
    Write-Host '  HA lights: enabled (reads through the AppDaemon plugin; no separate host)'
} else {
    Write-Host '  HA lights: disabled in profile (ha_light_enabled: false is generated)'
}
if ($Settings.integrations.hyperhdr.enabled) {
    $hyperhdr = $Settings.integrations.hyperhdr
    $hostPort = Split-HostPort -Value $hyperhdr.host
    $hyperhdrPort = if ($null -ne $hostPort[1]) { $hostPort[1] } else { 8090 }
    if (Test-TcpReachable -TargetHost $hostPort[0] -Port $hyperhdrPort) {
        Write-Host "  hyperHDR $($hyperhdr.host): reachable (tcp/$hyperhdrPort)"
    } else {
        $preflightErrors += "hyperHDR $($hyperhdr.host) is not reachable on tcp/$hyperhdrPort - contention probes will fail open."
    }
}
foreach ($warning in $preflightWarnings) {
    Write-Warning $warning
}
if ($preflightErrors.Count -gt 0 -and $Apply) {
    throw ("Provider preflight FAILED (-Apply): " + ($preflightErrors -join ' | '))
}
if ($preflightErrors.Count -gt 0) {
    Write-Warning ("Preflight reachability problems (continuing, read-only): " + ($preflightErrors -join ' | '))
}

Write-Host ''
Write-Host '== 6. apps.yaml scene_studio block (you merge this yourself) =='
& python $ProfileTool render-apps-yaml --profile $Profile
Write-Host ''
    Write-Host '  Secrets stay NAMES: put the real values in the add-on secrets.yaml'
    Write-Host '  (template: docs/installation/APPDAEMON_SECRETS_EXAMPLE.yaml in this bundle).'

if (-not $Apply) {
    Write-Host ''
    Write-Host '== Preflight summary (read-only; nothing was written) =='
    if ($env:SCENE_STUDIO_HA_TOKEN) {
        Write-Host '  SCENE_STUDIO_HA_TOKEN: set'
    } else {
        Write-Host '  SCENE_STUDIO_HA_TOKEN: NOT set (required for -Apply health checks)'
    }
    Write-Host 'Preflight OK. Re-run with -Apply to install.'
    if ($anyFresh) {
        Write-Host '  Fresh install: the deployers will run with -FreshInstall; the backend must come up in registry_admin.'
    }
    return
}

Write-Host ''
Write-Host '== Install (-Apply) =='
if (-not $env:SCENE_STUDIO_HA_TOKEN) {
    Write-Warning 'SCENE_STUDIO_HA_TOKEN is not set: the deployers'' runtime-context health check may fail.'
}
$env:SCENE_STUDIO_HA_URL = $HaUrl

$deployerArgs = @('-HaHost', $HaHost, '-AddonConfigRoot', $AddonConfigRoot, '-HaUrl', $HaUrl)
if (-not $SkipBackend) {
    Write-Host '-- Backend (Scene Studio Python package; backs up on upgrade, restarts AppDaemon) --'
    $backendArgs = @($deployerArgs + '-Apply')
    if ($SshPort -gt 0) { $backendArgs += @('-SshPort', [string]$SshPort) }
    if ($backendFresh) { $backendArgs += '-FreshInstall' }
    & pwsh -NoProfile -ExecutionPolicy Bypass -File $BackendDeployer @backendArgs
    if ($LASTEXITCODE -ne 0) {
        throw 'Backend deploy failed; the deployer restored the prior state. Fix the reported problem and re-run.'
    }
}
if (-not $SkipWorkbench) {
    Write-Host '-- Workbench (static SPA from the prebuilt bundle; no npm, no restart) --'
    $workbenchArgs = @($deployerArgs + '-Apply', '-Prebuilt')
    if ($SshPort -gt 0) { $workbenchArgs += @('-SshPort', [string]$SshPort) }
    if ($workbenchFresh) { $workbenchArgs += '-FreshInstall' }
    & pwsh -NoProfile -ExecutionPolicy Bypass -File $WorkbenchDeployer @workbenchArgs
    if ($LASTEXITCODE -ne 0) {
        throw 'Workbench deploy failed; the deployer restored the prior state. Fix the reported problem and re-run.'
    }
}

Write-Host ''
Write-Host '== Install complete =='
Write-Host "  1. Confirm the apps.yaml scene_studio block from step 6 is merged and AppDaemon restarted."
Write-Host "  2. Open the Workbench: http://<ha-host>:<appdaemon-port>/local/scene_studio/"
Write-Host "     (the Setup view appears while the registry is empty or the runtime is registry_admin)."
Write-Host '  3. Run discovery, adopt your fixtures, create targets, author + preview a scene.'
Write-Host "  4. Only when the registry is deliberately ready: set registry_admin: false / read_only: false"
Write-Host '     in apps.yaml and restart AppDaemon. Provider writes stay blocked until then.'
Write-Host "  Rollback: each deployer keeps a timestamped backup under $AddonConfigRoot/backups/;"
Write-Host '  a failed FIRST install is quarantined automatically back to the absent state. Removing the'
Write-Host '  scene_studio app block from apps.yaml and restarting AppDaemon disables Scene Studio without'
Write-Host '  touching your store (preserved at the configured store_root).'
