<#
.SYNOPSIS
  Verify a Scene Studio release tree against its MANIFEST.sha256.

.DESCRIPTION
  Fails BEFORE any remote contact when the tree has been changed, lost a
  file, or gained an unlisted payload: every manifest line must match an
  existing file byte-for-byte (SHA-256 + size), and every file under the
  release root (except the manifest itself) must be listed. Exits 0 only for
  a byte-exact release tree.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ReleaseRoot
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ReleaseRoot = (Resolve-Path -LiteralPath $ReleaseRoot).Path.TrimEnd('\', '/')

$manifestPath = Join-Path $ReleaseRoot 'MANIFEST.sha256'
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    throw "Release manifest missing: $manifestPath"
}

$expected = @{}
foreach ($rawLine in Get-Content -LiteralPath $manifestPath) {
    $line = $rawLine.Trim()
    if (-not $line -or $line.StartsWith('#')) { continue }
    # canonical writer: "<sha256>  <size>  <relative/path>"
    $parts = $line -split '\s+', 3
    if ($parts.Count -ne 3) {
        throw "Malformed manifest line: $line"
    }
    $hash = $parts[0].ToLowerInvariant()
    if ($hash -notmatch '^[0-9a-f]{64}$') {
        throw "Malformed sha256 in manifest line: $line"
    }
    if ($expected.ContainsKey($parts[2])) {
        throw "Duplicate manifest entry: $($parts[2])"
    }
    $expected[$parts[2]] = @{ Sha256 = $hash; Size = [long]$parts[1] }
}
if ($expected.Count -eq 0) {
    throw "Release manifest lists no files: $manifestPath"
}

# 1. every manifest entry matches an existing file, byte for byte.
foreach ($entry in $expected.Keys) {
    if ($entry -match '(^|/)\.\.(/|$)|\\|:' -or [System.IO.Path]::IsPathRooted($entry)) {
        throw "Unsafe manifest path: $entry"
    }
    $path = Join-Path $ReleaseRoot ($entry -replace '/', [System.IO.Path]::DirectorySeparatorChar)
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Manifest file missing from release tree: $entry"
    }
    $file = Get-Item -LiteralPath $path
    if ($file.Length -ne $expected[$entry].Size) {
        throw "Size mismatch for $entry : manifest $($expected[$entry].Size), actual $($file.Length)"
    }
    $actual = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $expected[$entry].Sha256) {
        throw "Hash mismatch for $entry : manifest $($expected[$entry].Sha256), actual $actual"
    }
}

# 2. no unlisted payload: everything under the release root must be listed.
# (Every MANIFEST.sha256 in the tree is verification metadata, not payload.)
$unlisted = @()
foreach ($file in Get-ChildItem -LiteralPath $ReleaseRoot -Recurse -File) {
    $relative = $file.FullName.Substring($ReleaseRoot.TrimEnd('\', '/').Length + 1).Replace('\', '/')
    if ($relative -eq 'MANIFEST.sha256') { continue }
    if (-not $expected.ContainsKey($relative)) {
        $unlisted += $relative
    }
}
if ($unlisted.Count -gt 0) {
    throw ("Release tree contains files not listed in MANIFEST.sha256: " + ($unlisted -join '; '))
}

Write-Host "Release manifest verified: $($expected.Count) files byte-exact, no unlisted payload."
exit 0
