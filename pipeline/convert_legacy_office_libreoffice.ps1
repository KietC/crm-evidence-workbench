# EN: Local-only convert legacy office libreoffice utility; preserve input evidence and keep source content out of logs.
# 中文：本地辅助工具；保留输入证据，不把源内容写入外部日志。
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Source,
    [Parameter(Mandatory = $true)][string]$OutputDir,
    [string]$Executable = $env:CRM_LIBREOFFICE
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$sourcePath = (Resolve-Path -LiteralPath $Source).Path
$targetRoot = [IO.Path]::GetFullPath($OutputDir)
[IO.Directory]::CreateDirectory($targetRoot) | Out-Null
$extension = [IO.Path]::GetExtension($sourcePath).ToLowerInvariant()
$mapping = @{
    '.doc' = @{ format = 'docx'; name = 'converted.docx' }
    '.xls' = @{ format = 'xlsx'; name = 'converted.xlsx' }
    '.ppt' = @{ format = 'pptx'; name = 'converted.pptx' }
}
if (-not $mapping.ContainsKey($extension)) { throw 'LEGACY_OFFICE_LIBREOFFICE_EXTENSION_UNSUPPORTED' }

# EN: Caller configuration or PATH selects LibreOffice; no workstation-specific location.
# 中文：通过调用参数或 PATH 选择 LibreOffice，不固定工作站安装位置。
$soffice = $Executable
if (-not $soffice) {
    $command = Get-Command soffice -ErrorAction SilentlyContinue
    if ($command) { $soffice = $command.Source }
}
if (-not $soffice -and $env:ProgramFiles) {
    $candidate = Join-Path $env:ProgramFiles 'LibreOffice\program\soffice.exe'
    if (Test-Path -LiteralPath $candidate -PathType Leaf) { $soffice = $candidate }
}
if (-not $soffice) { throw 'LIBREOFFICE_EXECUTABLE_MISSING' }
if (-not (Test-Path -LiteralPath $soffice -PathType Leaf)) { throw 'LIBREOFFICE_EXECUTABLE_MISSING' }
$temporaryRoot = Join-Path $targetRoot ('.libreoffice_' + [Guid]::NewGuid().ToString('N'))
$convertedRoot = Join-Path $temporaryRoot 'converted'
$profileRoot = Join-Path $temporaryRoot 'profile'
[IO.Directory]::CreateDirectory($convertedRoot) | Out-Null
[IO.Directory]::CreateDirectory($profileRoot) | Out-Null
$resolvedTemporary = [IO.Path]::GetFullPath($temporaryRoot)
if (-not $resolvedTemporary.StartsWith($targetRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'LIBREOFFICE_TEMP_SCOPE_INVALID'
}

try {
    $profileUri = [Uri]::new($profileRoot).AbsoluteUri
    & $soffice --headless --nologo --nodefault --nolockcheck --norestore `
        "-env:UserInstallation=$profileUri" --convert-to $mapping[$extension].format `
        --outdir $convertedRoot $sourcePath | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "LIBREOFFICE_EXIT_$LASTEXITCODE" }

    $deadline = [DateTimeOffset]::UtcNow.AddSeconds(30)
    $candidate = $null
    $previousSize = -1L
    $stablePolls = 0
    while ([DateTimeOffset]::UtcNow -lt $deadline) {
        $matches = @(Get-ChildItem -LiteralPath $convertedRoot -File -ErrorAction SilentlyContinue |
            Where-Object { $_.Extension -ieq ('.' + $mapping[$extension].format) })
        if ($matches.Count -eq 1) {
            $candidate = $matches[0]
            $size = [int64]$candidate.Length
            if ($size -gt 0 -and $size -eq $previousSize) { $stablePolls++ } else { $stablePolls = 0 }
            $previousSize = $size
            if ($stablePolls -ge 2) { break }
        }
        Start-Sleep -Milliseconds 250
    }
    if ($null -eq $candidate -or $stablePolls -lt 2) { throw 'LIBREOFFICE_OUTPUT_NOT_STABLE' }
    $destination = Join-Path $targetRoot $mapping[$extension].name
    if (Test-Path -LiteralPath $destination) { Remove-Item -LiteralPath $destination -Force }
    Move-Item -LiteralPath $candidate.FullName -Destination $destination
    if (-not (Test-Path -LiteralPath $destination -PathType Leaf) -or (Get-Item -LiteralPath $destination).Length -le 0) {
        throw 'LIBREOFFICE_OUTPUT_EMPTY'
    }
}
finally {
    if (Test-Path -LiteralPath $temporaryRoot) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

