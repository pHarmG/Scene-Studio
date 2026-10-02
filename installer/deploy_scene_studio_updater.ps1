#Requires -Version 7.0
<# Independent companion provisioning. Only this external deployer replaces
the supervisor trust boundary. Default is read-only. The wizard reviews its
two files, managed app block and AppDaemon restart before invoking -Apply. #>
[CmdletBinding()]
param([switch]$Apply, [string]$HaHost, [int]$SshPort = 0,
      [string]$AddonConfigRoot, [string]$HaUrl)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($AddonConfigRoot -notmatch '^/addon_configs/[a-z0-9_]+$') { throw 'Invalid AppDaemon config root.' }
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$files = @{
    'scene_studio_update_supervisor.py' = Join-Path $PSScriptRoot 'runtime/scene_studio_update_supervisor.py'
    'scene_studio_release.py' = Join-Path $root 'backend/src/scene_studio/release_trust.py'
}
$sshArgs = @('-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10')
if ($SshPort -gt 0) { $sshArgs += @('-p', [string]$SshPort) }
function Remote([string]$Command) {
    $result = & ssh @sshArgs $HaHost $Command
    if ($LASTEXITCODE -ne 0) { throw 'Updater remote operation failed.' }
    return ($result -join "`n")
}
$apps = "$AddonConfigRoot/apps"
$null = Remote "test -d '$apps'; test ! -L '$apps'; stat -c '%u:%g' '$apps'"
foreach ($name in $files.Keys) {
    if (-not (Test-Path -LiteralPath $files[$name])) { throw 'Companion source missing.' }
    $null = Remote "test ! -L '$apps/$name'; if test -f '$apps/$name'; then echo present; else echo absent; fi"
}
$journalPath = "$AddonConfigRoot/scene_studio_updates/transaction.json"
$journalText = Remote "if test -f '$journalPath'; then cat '$journalPath'; fi"
if ($journalText) {
    try { $journalState = $journalText | ConvertFrom-Json } catch { throw 'Updater recovery journal is invalid; recover before provisioning.' }
    if ($journalState.state -notin @('idle', 'succeeded', 'failed') -or ($journalState.PSObject.Properties.Name -contains 'recovery_required' -and $journalState.recovery_required)) {
        throw 'An update is pending or requires recovery; companion provisioning is blocked.'
    }
}
if (-not $Apply) { Write-Host 'Updater preflight complete (read-only).'; return }
if (-not $env:SCENE_STUDIO_HA_TOKEN) { throw 'SCENE_STUDIO_HA_TOKEN is required for companion provisioning.' }
$slug = Split-Path $AddonConfigRoot -Leaf
$backup = "$AddonConfigRoot/backups/scene-studio-updater-$([Guid]::NewGuid().ToString('N'))"
$stage = "$backup/stage"
$owner = (Remote "stat -c '%u:%g' '$apps'").Trim()
if ($owner -notmatch '^\d+:\d+$') { throw 'Cannot determine AppDaemon ownership.' }
$headers = @{Authorization="Bearer $env:SCENE_STUDIO_HA_TOKEN"}
function Restart-Addon {
    try {
        Invoke-RestMethod -Uri "$($HaUrl.TrimEnd('/'))/api/services/hassio/addon_restart" -Method Post -Headers $headers -ContentType 'application/json' -Body (@{addon=$slug} | ConvertTo-Json -Compress) -TimeoutSec 20 | Out-Null
    } catch { throw 'AppDaemon restart request failed.' }
}
$intent = $false
try {
    $null = Remote "set -eu; test ! -e '$backup'; sudo install -d -m 750 '$backup'; sudo install -d -m 755 '$stage'"
    foreach ($name in $files.Keys) {
        $null = Remote "set -eu; if test -f '$apps/$name'; then sudo cp -a '$apps/$name' '$backup/$name'; sudo cmp '$apps/$name' '$backup/$name'; else sudo touch '$backup/$name.absent'; fi"
        $encoded = [Convert]::ToBase64String([IO.File]::ReadAllBytes($files[$name]))
        $encoded | & ssh @sshArgs $HaHost "base64 -d | sudo tee '$stage/$name' > /dev/null"
        if ($LASTEXITCODE -ne 0) { throw 'Companion staging failed.' }
        $sha = (Get-FileHash -LiteralPath $files[$name] -Algorithm SHA256).Hash.ToLowerInvariant()
        if ((Remote "sudo sha256sum '$stage/$name' | cut -d ' ' -f1").Trim() -ne $sha) { throw 'Companion staging hash mismatch.' }
    }
    $intent = $true
    foreach ($name in $files.Keys) {
        $sha = (Get-FileHash -LiteralPath $files[$name] -Algorithm SHA256).Hash.ToLowerInvariant()
        $null = Remote "set -eu; sudo chown '$owner' '$stage/$name'; sudo chmod 644 '$stage/$name'; sudo mv '$stage/$name' '$apps/$name'"
        if ((Remote "sha256sum '$apps/$name' | cut -d ' ' -f1").Trim() -ne $sha) { throw 'Companion activation hash mismatch.' }
    }
    Restart-Addon
    $healthy = $false
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        try {
            $raw = Remote 'curl -fsS --max-time 5 -X POST -H ''Content-Type: application/json'' --data ''{"method":"GET","path":"/update/status"}'' http://127.0.0.1:5050/api/appdaemon/scene_studio_update_api'
            $response = $raw | ConvertFrom-Json
            if ($response.status -eq 200 -and $response.body.state -in @('idle', 'succeeded', 'failed') -and -not ($response.body.PSObject.Properties.Name -contains 'recovery_required' -and $response.body.recovery_required)) { $healthy = $true; break }
        } catch { }
        Start-Sleep -Seconds 1
    }
    if (-not $healthy) { throw 'Companion did not become healthy after restart.' }
} catch {
    if ($intent) {
        foreach ($name in $files.Keys) {
            $null = Remote "set -eu; if sudo test -f '$backup/$name'; then sudo cp -a '$backup/$name' '$apps/$name'; else sudo rm -f '$apps/$name'; fi"
        }
        Restart-Addon
        # Verify restored bytes/absence after restart. Keep backups regardless.
        foreach ($name in $files.Keys) {
            $null = Remote "set -eu; if sudo test -f '$backup/$name'; then sudo cmp '$backup/$name' '$apps/$name'; else test ! -e '$apps/$name'; fi"
        }
    }
    throw 'Companion provisioning failed; prior companion files restored when activation began. Backup retained.'
}
Write-Host 'Independent updater provisioned and API verified. Backup retained.'
