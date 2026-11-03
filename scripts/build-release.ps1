[CmdletBinding()]
param(
    [string]$Version,
    [string]$OutputDirectory
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
Add-Type -AssemblyName System.IO.Compression.FileSystem

$ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $ProjectRoot "dist"
}
$OutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
[System.IO.Directory]::CreateDirectory($OutputDirectory) | Out-Null

if (-not $Version) {
    $initText = Get-Content -LiteralPath (Join-Path $ProjectRoot "gomoku_assistant\__init__.py") -Raw
    $match = [regex]::Match($initText, '__version__\s*=\s*"([^"]+)"')
    if (-not $match.Success) {
        throw "Could not read __version__ from gomoku_assistant/__init__.py"
    }
    $Version = $match.Groups[1].Value
}
if ($Version -notmatch '^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$') {
    throw "Version must be a semantic version without a leading v: $Version"
}

$ReleaseName = "gomoku-screen-assistant-v$Version-windows-x64-python311"
$ArchivePath = Join-Path $OutputDirectory "$ReleaseName.zip"
$SourceAssetName = "rapfi-source-250615.zip"
$SourceAssetPath = Join-Path $OutputDirectory $SourceAssetName
$StageRoot = Join-Path $OutputDirectory (".stage-" + [guid]::NewGuid().ToString("N"))
$ReleaseRoot = Join-Path $StageRoot $ReleaseName
$VerifyRoot = Join-Path $StageRoot "verify"

$OutputPrefix = $OutputDirectory.TrimEnd('\') + '\'
if (-not $StageRoot.StartsWith($OutputPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to create a staging directory outside the output directory."
}

function Copy-ProjectFile {
    param([Parameter(Mandatory = $true)][string]$RelativePath)

    $Source = Join-Path $ProjectRoot $RelativePath
    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
        throw "Required release file is missing: $RelativePath"
    }
    $Destination = Join-Path $ReleaseRoot $RelativePath
    [System.IO.Directory]::CreateDirectory((Split-Path -Parent $Destination)) | Out-Null
    Copy-Item -LiteralPath $Source -Destination $Destination
}

$RapfiHashes = [ordered]@{
    "pbrain-rapfi-windows-avx2.exe" = "1E02497433FB765626F954C895B0B92E057AC12A76C76318ABE74D4050914E8E"
    "pbrain-rapfi-windows-sse.exe" = "DC98082D74E862547FD7F45130399782F21F6023AE15676A2AD471ECD1DACA4C"
    "mix9svqfreestyle_bsmix.bin.lz4" = "6BC0D1B0FF8E1D857F7F412923CD458F38F1A435087C91AE71FEC3676C23EF62"
    "model210901.bin" = "2FA08CB15CF240D9533548C3E011B03D46809F6858362E97EE873D0632AA2015"
    "rapfi-source-250615.zip" = "A2A410DE094F5813808CB99AC278C0180A0F26694E5F015A87FAE082B22D2EFC"
}

foreach ($Asset in $RapfiHashes.GetEnumerator()) {
    $AssetPath = Join-Path $ProjectRoot ("vendor\rapfi\" + $Asset.Key)
    if (-not (Test-Path -LiteralPath $AssetPath -PathType Leaf)) {
        throw "Required Rapfi asset is missing: $($Asset.Key)"
    }
    $ActualHash = (Get-FileHash -LiteralPath $AssetPath -Algorithm SHA256).Hash
    if ($ActualHash -ne $Asset.Value) {
        throw "Rapfi asset hash mismatch: $($Asset.Key)"
    }
}

try {
    [System.IO.Directory]::CreateDirectory($ReleaseRoot) | Out-Null

    @(
        "app.py",
        "start.bat",
        "requirements.txt",
        "README.md",
        "README.en.md",
        "LICENSE",
        "CHANGELOG.md",
        "SECURITY.md",
        "docs\screenshot.png"
    ) | ForEach-Object { Copy-ProjectFile $_ }

    Get-ChildItem -LiteralPath (Join-Path $ProjectRoot "gomoku_assistant") -Filter "*.py" -File |
        Sort-Object Name |
        ForEach-Object { Copy-ProjectFile ("gomoku_assistant\" + $_.Name) }

    @(
        "AUTHORS",
        "config.toml",
        "COPYING.txt",
        "mix9svqfreestyle_bsmix.bin.lz4",
        "model210901.bin",
        "NETWORK_WEIGHTS_CC0.txt",
        "pbrain-rapfi-windows-avx2.exe",
        "pbrain-rapfi-windows-sse.exe",
        "rapfi-source-250615.zip",
        "THIRD_PARTY_NOTICES.md"
    ) | ForEach-Object { Copy-ProjectFile ("vendor\rapfi\" + $_) }

    if (Test-Path -LiteralPath $ArchivePath) {
        Remove-Item -LiteralPath $ArchivePath -Force
    }
    Compress-Archive -LiteralPath $ReleaseRoot -DestinationPath $ArchivePath -CompressionLevel Optimal
    Copy-Item -LiteralPath (Join-Path $ProjectRoot "vendor\rapfi\$SourceAssetName") -Destination $SourceAssetPath -Force

    [System.IO.Compression.ZipFile]::ExtractToDirectory($ArchivePath, $VerifyRoot)
    $ExtractedRoot = Join-Path $VerifyRoot $ReleaseName
    if (-not (Test-Path -LiteralPath (Join-Path $ExtractedRoot "app.py") -PathType Leaf)) {
        throw "The release archive does not contain the expected top-level directory."
    }

    Push-Location $ExtractedRoot
    try {
        & py -3.11 "app.py" "--smoke-test"
        if ($LASTEXITCODE -ne 0) {
            throw "GUI smoke test failed in the extracted release."
        }

        $RapfiSmoke = "import numpy as np; from gomoku_assistant.engine import BLACK; from gomoku_assistant.rapfi import RapfiEngine; e=RapfiEngine(think_time_ms=200,threads=1,memory_mb=96); m=e.best_move(np.zeros((15,15),dtype=np.int8),BLACK); e.close(); assert m==(7,7),m"
        & py -3.11 -c $RapfiSmoke
        if ($LASTEXITCODE -ne 0) {
            throw "Rapfi smoke test failed in the extracted release."
        }
    }
    finally {
        Pop-Location
    }

    $ArchiveHash = (Get-FileHash -LiteralPath $ArchivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    $SourceHash = (Get-FileHash -LiteralPath $SourceAssetPath -Algorithm SHA256).Hash.ToLowerInvariant()
    @(
        "$ArchiveHash  $([System.IO.Path]::GetFileName($ArchivePath))",
        "$SourceHash  $SourceAssetName"
    ) | Set-Content -LiteralPath (Join-Path $OutputDirectory "SHA256SUMS.txt") -Encoding ascii

    Write-Host "Built $ArchivePath"
    Write-Host "Wrote $(Join-Path $OutputDirectory 'SHA256SUMS.txt')"
}
finally {
    if (Test-Path -LiteralPath $StageRoot) {
        [System.IO.Directory]::Delete($StageRoot, $true)
    }
}
