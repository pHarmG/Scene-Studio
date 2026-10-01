#Requires -Version 7.0
<#
Release bootstrap only. Download this asset from GitHub Releases, then run
pwsh ./Get-SceneStudio.ps1. Optional private access: SCENE_STUDIO_GITHUB_TOKEN
in the process environment (Contents read permission). Never installs directly.
#>
[CmdletBinding()]
param([string]$Version, [switch]$DownloadOnly)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script:Repository = 'pHarmG/Scene-Studio'

function Invoke-GithubDownload {
    param([string]$Url, [string]$Accept, [string]$OutFile)
    $handler = [System.Net.Http.HttpClientHandler]::new()
    $handler.AllowAutoRedirect = $false
    $client = [System.Net.Http.HttpClient]::new($handler)
    $client.Timeout = [TimeSpan]::FromSeconds(120)
    try {
        for ($hop = 0; $hop -lt 5; $hop++) {
            $uri = [Uri]$Url
            if ($uri.Scheme -ne 'https' -or $uri.UserInfo -or $uri.Port -ne 443 -or
                ($uri.Host -notin @('api.github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com', 'github.com'))) {
                throw 'Untrusted release download URL.'
            }
            $request = [System.Net.Http.HttpRequestMessage]::new([System.Net.Http.HttpMethod]::Get, $uri)
            $request.Headers.Add('User-Agent', 'Scene-Studio')
            $request.Headers.Add('Accept', $Accept)
            # Only the initial API request may carry authentication.
            if ($hop -eq 0 -and $uri.Host -eq 'api.github.com' -and $env:SCENE_STUDIO_GITHUB_TOKEN) {
                $request.Headers.Add('Authorization', "Bearer $env:SCENE_STUDIO_GITHUB_TOKEN")
            }
            $response = $client.SendAsync($request).GetAwaiter().GetResult()
            try {
                $code = [int]$response.StatusCode
                if ($code -in @(301, 302, 303, 307, 308)) {
                    $Url = [Uri]::new($uri, $response.Headers.Location).AbsoluteUri
                    continue
                }
                if ($code -ne 200) { throw 'Release download unavailable.' }
                if ($OutFile) {
                    $stream = [IO.File]::Create($OutFile)
                    try { $response.Content.CopyToAsync($stream).GetAwaiter().GetResult() } finally { $stream.Dispose() }
                    return
                }
                return $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
            } finally { $response.Dispose(); $request.Dispose() }
        }
        throw 'Too many release download redirects.'
    } catch {
        # Do not include network exceptions, headers, URLs with signed queries,
        # or upstream bodies in logs / support reports.
        throw 'Cannot retrieve GitHub Release. For private access set SCENE_STUDIO_GITHUB_TOKEN (Contents read), or download the full ZIP and SHA256SUMS.txt manually from the signed-in Release page.'
    } finally { $client.Dispose(); $handler.Dispose() }
}

function Get-GithubRelease {
    param([string]$Tag)
    $suffix = if ($Tag) { "tags/$Tag" } else { 'latest' }
    try {
        return (Invoke-GithubDownload -Url "https://api.github.com/repos/$script:Repository/releases/$suffix" -Accept 'application/vnd.github+json' | ConvertFrom-Json)
    } catch {
        throw 'Release lookup unavailable. Sign in to GitHub Releases and download the full ZIP plus SHA256SUMS.txt, or configure optional SCENE_STUDIO_GITHUB_TOKEN with Contents read access.'
    }
}

function Save-GithubAsset {
    param($Asset, [string]$Path)
    $id = [long]$Asset.id
    if ($id -le 0) { throw 'Invalid release asset identity.' }
    Invoke-GithubDownload -Url "https://api.github.com/repos/$script:Repository/releases/assets/$id" -Accept 'application/octet-stream' -OutFile $Path
}

function Get-VerifiedSceneStudioRelease {
    param([string]$RequestedVersion)
    if ($RequestedVersion -and $RequestedVersion -notmatch '^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$') { throw 'Version must be a stable semantic version, e.g. 0.1.0.' }
    $tag = if ($RequestedVersion) { "v$RequestedVersion" } else { '' }
    $release = Get-GithubRelease -Tag $tag
    if ($release.draft -or $release.prerelease -or $release.tag_name -notmatch '^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$') { throw 'No compatible stable release.' }
    if ($tag -and $release.tag_name -ne $tag) { throw 'Requested release mismatch.' }
    $version = $release.tag_name.Substring(1)
    $zipName = "Scene-Studio-v$version.zip"
    $zipAsset = @($release.assets | Where-Object name -eq $zipName)
    $sumAsset = @($release.assets | Where-Object name -eq 'SHA256SUMS.txt')
    if ($zipAsset.Count -ne 1 -or $sumAsset.Count -ne 1) { throw 'Release must contain exactly one product ZIP and SHA256SUMS.txt.' }
    $temp = Join-Path ([IO.Path]::GetTempPath()) ('scene-studio-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $temp | Out-Null
    $zip = Join-Path $temp $zipName
    $sums = Join-Path $temp 'SHA256SUMS.txt'
    Save-GithubAsset -Asset $sumAsset[0] -Path $sums
    Save-GithubAsset -Asset $zipAsset[0] -Path $zip
    $matches = @(Get-Content -LiteralPath $sums | Where-Object { $_ -match ('^[0-9a-fA-F]{64}  ' + [Regex]::Escape($zipName) + '$') })
    if ($matches.Count -ne 1) { throw 'Missing or ambiguous release checksum.' }
    $expected = $matches[0].Substring(0, 64).ToLowerInvariant()
    if ((Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expected) { throw 'Release ZIP checksum verification failed. Installer was not executed.' }
    # Validate paths before extracting any entry, including Windows separators.
    $archive = [IO.Compression.ZipFile]::OpenRead($zip)
    try {
        $names = @{}
        foreach ($entry in $archive.Entries) {
            $name = $entry.FullName.Replace('\', '/')
            if ($name -notmatch '^scene-studio-release/' -or $name -match '(^|/)\.\.(/|$)|:|(^|/)\.(/|$)' -or $names.ContainsKey($name)) { throw 'Unsafe or duplicate ZIP entry.' }
            $names[$name] = $true
        }
    } finally { $archive.Dispose() }
    $extract = Join-Path $temp 'verified'
    Expand-Archive -LiteralPath $zip -DestinationPath $extract
    $root = Join-Path $extract 'scene-studio-release'
    # ZIP checksum authenticates the verifier too; verify EVERY extracted file
    # before delegating to the canonical wizard.
    & pwsh -NoProfile -File (Join-Path $root 'installer/verify_release_manifest.ps1') -ReleaseRoot $root | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'Release manifest verification failed. Installer was not executed.' }
    $build = Get-Content -LiteralPath (Join-Path $root 'BUILD.json') -Raw | ConvertFrom-Json
    if ($build.version -ne $version -or $build.channel -ne 'release' -or $build.dirty -or $build.tag -ne $release.tag_name -or $build.source_sha -notmatch '^[0-9a-f]{40}$') { throw 'Release build identity mismatch.' }
    Write-Host "Verified release v$version at $root"
    return $root
}

function Start-SceneStudioInstaller {
    param([string]$Root)
    & pwsh -NoProfile -File (Join-Path $Root 'Install-SceneStudio.ps1')
    if ($LASTEXITCODE -ne 0) { throw 'Guided installer failed. See its sanitized support report in the retained release directory.' }
}

function Invoke-SceneStudioBootstrap {
    param([string]$RequestedVersion, [switch]$OnlyDownload)
    $root = Get-VerifiedSceneStudioRelease -RequestedVersion $RequestedVersion
    if (-not $OnlyDownload) { Start-SceneStudioInstaller -Root $root }
}

if ($MyInvocation.InvocationName -ne '.') {
    Invoke-SceneStudioBootstrap -RequestedVersion $Version -OnlyDownload:$DownloadOnly
}
