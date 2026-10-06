# EN: Local-only word com export worker utility; preserve input evidence and keep source content out of logs.
# 中文：本地辅助工具；保留输入证据，不把源内容写入外部日志。
param(
    [Parameter(Mandatory = $true)][string]$InstructionJson,
    [Parameter(Mandatory = $true)][ValidatePattern('^[A-Fa-f0-9]{64}$')][string]$ExpectedInstructionSha256
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$word = $null
$document = $null

function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToUpperInvariant()
}

function Add-Phase([string]$ProgressPath, [string]$Phase) {
    [IO.File]::AppendAllText(
        $ProgressPath,
        $Phase + [Environment]::NewLine,
        [Text.UTF8Encoding]::new($false)
    )
}

try {
    $instructionPath = (Resolve-Path -LiteralPath $InstructionJson).Path
    $instructionSha256 = Get-Sha256 $instructionPath
    if ($instructionSha256 -cne $ExpectedInstructionSha256.ToUpperInvariant()) {
        throw 'WORD_COM_WORKER_INSTRUCTION_HASH_MISMATCH'
    }
    $instruction = Get-Content -LiteralPath $instructionPath -Raw | ConvertFrom-Json
    $keys = @($instruction.PSObject.Properties.Name | Sort-Object)
    $expectedKeys = @('expected_input_sha256', 'input_docx', 'output_pdf', 'progress_log', 'schema')
    if (($keys -join "`n") -cne (($expectedKeys | Sort-Object) -join "`n")) {
        throw 'WORD_COM_WORKER_INSTRUCTION_SCHEMA_INVALID'
    }
    if ($instruction.schema -cne 'okki.customer_qa.word_com_worker_instruction.v1') {
        throw 'WORD_COM_WORKER_INSTRUCTION_SCHEMA_INVALID'
    }
    $inputPath = (Resolve-Path -LiteralPath ([string]$instruction.input_docx)).Path
    $outputPath = [IO.Path]::GetFullPath([string]$instruction.output_pdf)
    $progressPath = [IO.Path]::GetFullPath([string]$instruction.progress_log)
    if (-not [IO.Path]::IsPathRooted($outputPath) -or -not [IO.Path]::IsPathRooted($progressPath)) {
        throw 'WORD_COM_WORKER_PATH_INVALID'
    }
    if ([IO.Path]::GetExtension($inputPath) -ine '.docx' -or [IO.Path]::GetExtension($outputPath) -ine '.pdf') {
        throw 'WORD_COM_WORKER_EXTENSION_INVALID'
    }
    if ($inputPath -ieq $outputPath -or $inputPath -ieq $progressPath -or $outputPath -ieq $progressPath) {
        throw 'WORD_COM_WORKER_PATH_COLLISION'
    }
    if (Test-Path -LiteralPath $outputPath) {
        throw 'WORD_COM_WORKER_OUTPUT_PREEXISTS'
    }
    if ([string]$instruction.expected_input_sha256 -cnotmatch '^[A-F0-9]{64}$') {
        throw 'WORD_COM_WORKER_INPUT_HASH_INVALID'
    }
    if ((Get-Sha256 $inputPath) -cne [string]$instruction.expected_input_sha256) {
        throw 'WORD_COM_WORKER_INPUT_HASH_MISMATCH'
    }
    [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($outputPath)) | Out-Null
    [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($progressPath)) | Out-Null

    Add-Phase $progressPath 'BEFORE_COM_CREATE'
    $word = New-Object -ComObject Word.Application
    Add-Phase $progressPath 'AFTER_COM_CREATE'
    $word.Visible = $false
    $word.DisplayAlerts = 0
    $word.AutomationSecurity = 3
    $word.Options.UpdateLinksAtOpen = $false
    $word.Options.SaveNormalPrompt = $false
    Add-Phase $progressPath 'BEFORE_DOCUMENT_OPEN'
    $document = $word.Documents.Open($inputPath, $false, $true, $false)
    Add-Phase $progressPath 'AFTER_DOCUMENT_OPEN'
    $document.ExportAsFixedFormat($outputPath, 17)
    Add-Phase $progressPath 'AFTER_PDF_EXPORT'
    $document.Close($false)
    [Runtime.InteropServices.Marshal]::FinalReleaseComObject($document) | Out-Null
    $document = $null
    $word.Quit()
    [Runtime.InteropServices.Marshal]::FinalReleaseComObject($word) | Out-Null
    $word = $null
    if ((Get-Sha256 $inputPath) -cne [string]$instruction.expected_input_sha256) {
        throw 'WORD_COM_WORKER_INPUT_CHANGED'
    }
    if ((Get-Sha256 $instructionPath) -cne $instructionSha256) {
        throw 'WORD_COM_WORKER_INSTRUCTION_CHANGED'
    }
    if (-not (Test-Path -LiteralPath $outputPath -PathType Leaf) -or (Get-Item -LiteralPath $outputPath).Length -le 0) {
        throw 'WORD_COM_WORKER_OUTPUT_EMPTY'
    }
    Add-Phase $progressPath 'DONE'
    exit 0
} catch {
    $code = if ($_.Exception.Message -cmatch '^[A-Z0-9_]+$') { $_.Exception.Message } else { 'WORD_COM_WORKER_FAILED' }
    [Console]::Error.WriteLine($code)
    exit 2
} finally {
    if ($document) {
        try { $document.Close($false) } catch {}
        try { [Runtime.InteropServices.Marshal]::FinalReleaseComObject($document) | Out-Null } catch {}
    }
    if ($word) {
        try { $word.Quit() } catch {}
        try { [Runtime.InteropServices.Marshal]::FinalReleaseComObject($word) | Out-Null } catch {}
    }
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
}
