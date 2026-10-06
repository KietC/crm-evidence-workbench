# EN: Local-only render docx with word utility; preserve input evidence and keep source content out of logs.
# 中文：本地辅助工具；保留输入证据，不把源内容写入外部日志。
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Document,
    [Parameter(Mandatory = $true)]
    [string]$Pdf,
    [Parameter(Mandatory = $true)]
    [string]$Verification
)

$ErrorActionPreference = 'Stop'
$documentPath = (Resolve-Path -LiteralPath $Document).Path
$pdfPath = [IO.Path]::GetFullPath($Pdf)
$verificationPath = [IO.Path]::GetFullPath($Verification)
New-Item -ItemType Directory -Path (Split-Path -Parent $pdfPath) -Force | Out-Null
New-Item -ItemType Directory -Path (Split-Path -Parent $verificationPath) -Force | Out-Null
$word = $null
$doc = $null

try {
    $word = New-Object -ComObject Word.Application
    $word.Visible = $false
    $word.DisplayAlerts = 0
    $word.AutomationSecurity = 3
    $doc = $word.Documents.Open($documentPath, $false, $true, $false)
    $pages = [int]$doc.ComputeStatistics(2)
    $paragraphs = [int]$doc.Paragraphs.Count
    $tables = [int]$doc.Tables.Count
    $doc.ExportAsFixedFormat($pdfPath, 17, $false, 0, 0, 1, [int]$doc.ComputeStatistics(2), 0, $true, $true, 1, $true, $true, $false)
    $result = [ordered]@{
        schema = 'okki.single_customer.word_render_verification.v1'
        status = if ((Test-Path -LiteralPath $pdfPath) -and (Get-Item -LiteralPath $pdfPath).Length -gt 0 -and $pages -gt 0) { 'PASS' } else { 'FAIL' }
        opened_read_only = [bool]$doc.ReadOnly
        pages = $pages
        paragraphs = $paragraphs
        tables = $tables
        docx_sha256 = (Get-FileHash -LiteralPath $documentPath -Algorithm SHA256).Hash
        pdf_bytes = (Get-Item -LiteralPath $pdfPath).Length
        pdf_sha256 = (Get-FileHash -LiteralPath $pdfPath -Algorithm SHA256).Hash
        verified_at_utc = (Get-Date).ToUniversalTime().ToString('o')
    }
    $result | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $verificationPath -Encoding UTF8
    Write-Output ($result | ConvertTo-Json -Compress)
    if ($result.status -ne 'PASS') { exit 2 }
}
finally {
    if ($doc) { $doc.Close($false) }
    if ($word) { $word.Quit() }
    if ($doc) { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($doc) }
    if ($word) { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($word) }
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
}
