# EN: Local-only export docx pdf utility; preserve input evidence and keep source content out of logs.
# 中文：本地辅助工具；保留输入证据，不把源内容写入外部日志。
param(
    [Parameter(Mandatory = $true)][ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')][string]$CustomerKey,
    [Parameter(Mandatory = $true)][ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')][string]$RunId,
    [Parameter(Mandatory = $true)][string]$InputDocx,
    [Parameter(Mandatory = $true)][string]$OutputPdf,
    [Parameter(Mandatory = $true)][string]$StatusJson,
    [Parameter(Mandatory = $true)][ValidatePattern('^[A-Fa-f0-9]{64}$')]
    [string]$ExpectedInputSha256,
    [Parameter(Mandatory = $true)][ValidatePattern('^[A-Fa-f0-9]{64}$')]
    [string]$ExpectedParentSha256,
    [string]$ProgressJson = '',
    [string]$WorkerScript = '',
    [Parameter(Mandatory = $true)][ValidatePattern('^[A-Fa-f0-9]{64}$')]
    [string]$ExpectedWorkerSha256,
    [string]$PowerShellExe = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe",
    [Parameter(Mandatory = $true)][ValidatePattern('^[A-Fa-f0-9]{64}$')]
    [string]$ExpectedPowerShellSha256,
    [ValidateRange(30, 3600)][int]$WorkerTimeoutSec = 600
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$utf8NoBom = [Text.UTF8Encoding]::new($false)
$started = Get-Date
$temporaryPdf = $null
$workerProcess = $null
$privateRoot = $null
$beforeWordPids = @()
$wordBaselineCaptured = $false

function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToUpperInvariant()
}

function Copy-InputLocked([string]$Source, [string]$Destination) {
    $sourceStream = $null
    $destinationStream = $null
    $hasher = $null
    try {
        $sourceStream = [IO.File]::Open($Source, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
        $hasher = [Security.Cryptography.SHA256]::Create()
        $hashBytes = $hasher.ComputeHash($sourceStream)
        $sourceHash = ([BitConverter]::ToString($hashBytes)).Replace('-', '')
        $sourceStream.Position = 0
        $destinationStream = [IO.File]::Open($Destination, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        $sourceStream.CopyTo($destinationStream)
        $destinationStream.Flush($true)
        return $sourceHash
    } finally {
        if ($destinationStream) { $destinationStream.Dispose() }
        if ($sourceStream) { $sourceStream.Dispose() }
        if ($hasher) { $hasher.Dispose() }
    }
}

function Stop-ExactProcessTree([int]$RootPid) {
    $rows = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Select-Object ProcessId, ParentProcessId)
    $children = @{}
    foreach ($row in $rows) {
        $parent = [int]$row.ParentProcessId
        if (-not $children.ContainsKey($parent)) { $children[$parent] = @() }
        $children[$parent] += [int]$row.ProcessId
    }
    $ordered = [Collections.Generic.List[int]]::new()
    function Visit([int]$CurrentProcessId) {
        if ($children.ContainsKey($CurrentProcessId)) {
            foreach ($child in @($children[$CurrentProcessId])) { Visit $child }
        }
        $ordered.Add($CurrentProcessId)
    }
    Visit $RootPid
    foreach ($pidValue in $ordered) {
        Stop-Process -Id $pidValue -Force -ErrorAction SilentlyContinue
    }
}

function Test-ProcessAlive([Diagnostics.Process]$Process) {
    if (-not $Process) { return $false }
    try { return -not $Process.HasExited } catch { return $false }
}

function Write-JsonAtomic([string]$Path, [object]$Value) {
    $full = [IO.Path]::GetFullPath($Path)
    $directory = [IO.Path]::GetDirectoryName($full)
    [IO.Directory]::CreateDirectory($directory) | Out-Null
    $temporary = Join-Path $directory ('.' + [IO.Path]::GetFileName($full) + '.' + [guid]::NewGuid().ToString('N') + '.tmp')
    $backup = Join-Path $directory ('.' + [IO.Path]::GetFileName($full) + '.' + [guid]::NewGuid().ToString('N') + '.bak')
    try {
        $json = $Value | ConvertTo-Json -Depth 10 -Compress
        [IO.File]::WriteAllText($temporary, $json, $utf8NoBom)
        if (Test-Path -LiteralPath $full -PathType Leaf) {
            [IO.File]::Replace($temporary, $full, $backup, $true)
            Remove-Item -LiteralPath $backup -Force -ErrorAction SilentlyContinue
        } else {
            [IO.File]::Move($temporary, $full)
        }
    } finally {
        if (Test-Path -LiteralPath $temporary -PathType Leaf) {
            Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
        }
        if (Test-Path -LiteralPath $backup -PathType Leaf) {
            Remove-Item -LiteralPath $backup -Force -ErrorAction SilentlyContinue
        }
    }
}

function Set-ExportPhase([string]$Phase, [string]$State = 'IN_PROGRESS') {
    if (-not $ProgressJson) { return }
    Write-JsonAtomic $ProgressJson ([ordered]@{
        schema = 'okki.customer_qa.word_pdf_export_progress.v1'
        status = $State
        phase = $Phase
        process_id = $PID
        observed_at_utc = [DateTime]::UtcNow.ToString('o')
    })
}

try {
    $inputPath = (Resolve-Path -LiteralPath $InputDocx).Path
    $outputPath = [IO.Path]::GetFullPath($OutputPdf)
    $statusPath = [IO.Path]::GetFullPath($StatusJson)
    $parentPath = (Resolve-Path -LiteralPath $PSCommandPath).Path
    $workerCandidate = if ($WorkerScript) { $WorkerScript } else { Join-Path $PSScriptRoot 'word_com_export_worker.ps1' }
    $workerPath = (Resolve-Path -LiteralPath $workerCandidate).Path
    $powerShellPath = (Resolve-Path -LiteralPath $PowerShellExe).Path
    $progressPath = if ($ProgressJson) { [IO.Path]::GetFullPath($ProgressJson) } else { '' }
    $uniquePaths = @($inputPath, $outputPath, $statusPath, $parentPath, $workerPath, $powerShellPath)
    if ($progressPath) { $uniquePaths += $progressPath }
    if (@($uniquePaths | Sort-Object -Unique).Count -ne $uniquePaths.Count) {
        throw 'WORD_EXPORT_PATH_COLLISION'
    }
    if ([IO.Path]::GetExtension($inputPath) -ine '.docx' -or [IO.Path]::GetExtension($outputPath) -ine '.pdf') {
        throw 'WORD_EXPORT_EXTENSION_INVALID'
    }
    if (Test-Path -LiteralPath $outputPath) {
        throw 'WORD_EXPORT_OUTPUT_PREEXISTS'
    }
    [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($outputPath)) | Out-Null
    [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($statusPath)) | Out-Null
    $parentSha256 = Get-Sha256 $parentPath
    $workerSha256 = Get-Sha256 $workerPath
    $powerShellSha256 = Get-Sha256 $powerShellPath
    if ($parentSha256 -cne $ExpectedParentSha256.ToUpperInvariant()) {
        throw 'WORD_EXPORT_PARENT_HASH_MISMATCH'
    }
    if ($workerSha256 -cne $ExpectedWorkerSha256.ToUpperInvariant()) {
        throw 'WORD_EXPORT_WORKER_HASH_MISMATCH'
    }
    if ($powerShellSha256 -cne $ExpectedPowerShellSha256.ToUpperInvariant()) {
        throw 'WORD_EXPORT_POWERSHELL_HASH_MISMATCH'
    }

    $nonce = [guid]::NewGuid().ToString('N')
    $temporaryPdf = Join-Path ([IO.Path]::GetDirectoryName($outputPath)) ('WORDPDF_' + $nonce + '.pdf')
    $privateRoot = Join-Path ([IO.Path]::GetDirectoryName($statusPath)) ('word_export_private_' + $nonce)
    [IO.Directory]::CreateDirectory($privateRoot) | Out-Null
    $stagedInputPath = Join-Path $privateRoot 'input.staged.docx'
    $inputSha256 = Copy-InputLocked $inputPath $stagedInputPath
    if ($inputSha256 -cne $ExpectedInputSha256.ToUpperInvariant()) {
        throw 'WORD_EXPORT_INPUT_HASH_MISMATCH'
    }
    if ((Get-Sha256 $stagedInputPath) -cne $inputSha256) {
        throw 'WORD_EXPORT_STAGED_INPUT_HASH_MISMATCH'
    }
    $instructionPath = Join-Path $privateRoot 'instruction.private.json'
    $workerProgressPath = Join-Path $privateRoot 'worker_progress.log'
    $workerStdoutPath = Join-Path $privateRoot 'worker_stdout.log'
    $workerStderrPath = Join-Path $privateRoot 'worker_stderr.log'
    Write-JsonAtomic $instructionPath ([ordered]@{
        schema = 'okki.customer_qa.word_com_worker_instruction.v1'
        input_docx = $stagedInputPath
        output_pdf = $temporaryPdf
        progress_log = $workerProgressPath
        expected_input_sha256 = $inputSha256
    })
    $instructionSha256 = Get-Sha256 $instructionPath

    Set-ExportPhase 'BEFORE_WORKER_START'
    $beforeWordPids = @(Get-Process WINWORD -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id)
    $wordBaselineCaptured = $true
    $processInfo = [Diagnostics.ProcessStartInfo]::new()
    $processInfo.FileName = $powerShellPath
    $processInfo.Arguments = '-NoProfile -NonInteractive -STA -ExecutionPolicy Bypass -File "' + $workerPath + '" -InstructionJson "' + $instructionPath + '" -ExpectedInstructionSha256 ' + $instructionSha256
    $processInfo.WorkingDirectory = $privateRoot
    $processInfo.UseShellExecute = $false
    $processInfo.CreateNoWindow = $true
    $processInfo.RedirectStandardOutput = $true
    $processInfo.RedirectStandardError = $true
    $workerProcess = [Diagnostics.Process]::new()
    $workerProcess.StartInfo = $processInfo
    if (-not $workerProcess.Start()) { throw 'WORD_EXPORT_WORKER_START_FAILED' }
    $stdoutTask = $workerProcess.StandardOutput.ReadToEndAsync()
    $stderrTask = $workerProcess.StandardError.ReadToEndAsync()
    Set-ExportPhase 'WORKER_RUNNING'
    $workerCompleted = $workerProcess.WaitForExit($WorkerTimeoutSec * 1000)
    if (-not $workerCompleted) {
        Stop-ExactProcessTree $workerProcess.Id
        throw 'WORD_EXPORT_WORKER_TIMEOUT'
    }
    $workerProcess.WaitForExit()
    $workerStdout = $stdoutTask.Result
    $workerStderr = $stderrTask.Result
    [IO.File]::WriteAllText($workerStdoutPath, $workerStdout, $utf8NoBom)
    [IO.File]::WriteAllText($workerStderrPath, $workerStderr, $utf8NoBom)
    $workerExitCode = $workerProcess.ExitCode
    if ($workerExitCode -ne 0) {
        throw 'WORD_EXPORT_WORKER_FAILED'
    }
    if ($workerStdout.Length -ne 0 -or $workerStderr.Length -ne 0) {
        throw 'WORD_EXPORT_WORKER_CONSOLE_NOT_EMPTY'
    }
    Set-ExportPhase 'WORKER_EXITED'

    $wordExitDeadline = (Get-Date).AddSeconds(15)
    do {
        $newWordPids = @(Get-Process WINWORD -ErrorAction SilentlyContinue | Where-Object { $_.Id -notin $beforeWordPids } | Select-Object -ExpandProperty Id)
        if ($newWordPids.Count -eq 0) { break }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $wordExitDeadline)
    if ($newWordPids.Count -ne 0) {
        throw 'WORD_EXPORT_WINWORD_RESIDUAL'
    }
    if ((Get-Sha256 $inputPath) -cne $inputSha256 -or (Get-Sha256 $stagedInputPath) -cne $inputSha256) {
        throw 'WORD_EXPORT_INPUT_CHANGED_DURING_EXPORT'
    }
    if ((Get-Sha256 $instructionPath) -cne $instructionSha256) {
        throw 'WORD_EXPORT_INSTRUCTION_CHANGED_DURING_EXPORT'
    }
    if ((Get-Sha256 $parentPath) -cne $parentSha256 -or (Get-Sha256 $workerPath) -cne $workerSha256 -or (Get-Sha256 $powerShellPath) -cne $powerShellSha256) {
        throw 'WORD_EXPORT_EXECUTABLE_CHANGED_DURING_EXPORT'
    }
    if (-not (Test-Path -LiteralPath $temporaryPdf -PathType Leaf) -or (Get-Item -LiteralPath $temporaryPdf).Length -le 0) {
        throw 'WORD_EXPORT_PDF_EMPTY'
    }
    $workerPhases = if (Test-Path -LiteralPath $workerProgressPath) {
        @((Get-Content -LiteralPath $workerProgressPath) | ForEach-Object { [string]$_ })
    } else { @() }
    $requiredPhases = @('BEFORE_COM_CREATE','AFTER_COM_CREATE','BEFORE_DOCUMENT_OPEN','AFTER_DOCUMENT_OPEN','AFTER_PDF_EXPORT','DONE')
    if (($workerPhases -join "`n") -cne ($requiredPhases -join "`n")) {
        throw 'WORD_EXPORT_WORKER_PHASE_SEQUENCE_INVALID'
    }
    [IO.File]::Move($temporaryPdf, $outputPath)
    $temporaryPdf = $null
    Write-JsonAtomic $statusPath ([ordered]@{
        schema = 'okki.customer_qa.word_pdf_export_receipt.v1'
        status = 'PASS_CUSTOMER_QA_WORD_PDF_EXPORT'
        customer_key = $CustomerKey
        run_id = $RunId
        counts = [ordered]@{ input_docx = 1; output_pdf = 1; residual_winword_pids = 0 }
        input = [ordered]@{
            original = [ordered]@{ path = $inputPath; bytes = (Get-Item -LiteralPath $inputPath).Length; sha256 = $inputSha256 }
            staged = [ordered]@{ path = $stagedInputPath; bytes = (Get-Item -LiteralPath $stagedInputPath).Length; sha256 = $inputSha256 }
        }
        output = [ordered]@{ path = $outputPath; bytes = (Get-Item -LiteralPath $outputPath).Length; sha256 = (Get-Sha256 $outputPath) }
        worker = [ordered]@{
            parent = [ordered]@{ path = $parentPath; bytes = (Get-Item -LiteralPath $parentPath).Length; sha256 = $parentSha256 }
            script = [ordered]@{ path = $workerPath; bytes = (Get-Item -LiteralPath $workerPath).Length; sha256 = $workerSha256 }
            powershell = [ordered]@{ path = $powerShellPath; bytes = (Get-Item -LiteralPath $powerShellPath).Length; sha256 = $powerShellSha256 }
            instruction = [ordered]@{ path = $instructionPath; bytes = (Get-Item -LiteralPath $instructionPath).Length; sha256 = $instructionSha256 }
            stdout = [ordered]@{ path = $workerStdoutPath; bytes = (Get-Item -LiteralPath $workerStdoutPath).Length; sha256 = (Get-Sha256 $workerStdoutPath) }
            stderr = [ordered]@{ path = $workerStderrPath; bytes = (Get-Item -LiteralPath $workerStderrPath).Length; sha256 = (Get-Sha256 $workerStderrPath) }
            phases = $workerPhases
            exit_code = $workerExitCode
            timeout_seconds = $WorkerTimeoutSec
            residual_winword_pids = 0
        }
        policy = [ordered]@{
            read_only = $true
            saved = $false
            macros_force_disabled = $true
            update_links_at_open = $false
            online_services_claimed_by_worker = $false
            office_network_isolation_requires_separate_runtime_authority = $true
        }
        elapsed_seconds = [math]::Round(((Get-Date) - $started).TotalSeconds, 2)
    })
    Set-ExportPhase 'COMPLETE' 'PASS'
} catch {
    $failureCode = if ($_.Exception.Message -cmatch '^[A-Z0-9_]+$') { $_.Exception.Message } else { 'WORD_EXPORT_FAILED' }
    if (Test-ProcessAlive $workerProcess) {
        Stop-ExactProcessTree $workerProcess.Id
        Start-Sleep -Milliseconds 300
    }
    $workerAliveAfterCleanup = Test-ProcessAlive $workerProcess
    $residualWordCount = if ($wordBaselineCaptured) {
        @(Get-Process WINWORD -ErrorAction SilentlyContinue | Where-Object { $_.Id -notin $beforeWordPids }).Count
    } else { 0 }
    Set-ExportPhase 'FAILED' 'FAIL'
    if ($StatusJson) {
        Write-JsonAtomic $StatusJson ([ordered]@{
            schema = 'okki.customer_qa.word_pdf_export_receipt.v1'
            status = 'FAIL_CUSTOMER_QA_WORD_PDF_EXPORT'
            customer_key = $CustomerKey
            run_id = $RunId
            error_code = $failureCode
            counts = [ordered]@{ residual_winword_pids = $residualWordCount }
            cleanup = [ordered]@{
                worker_process_alive = $workerAliveAfterCleanup
                office_runtime_authority_cleanup_required = ($residualWordCount -ne 0)
            }
            logs = [ordered]@{
                stdout = if ($workerStdoutPath -and (Test-Path -LiteralPath $workerStdoutPath -PathType Leaf)) { [ordered]@{ path = $workerStdoutPath; bytes = (Get-Item -LiteralPath $workerStdoutPath).Length; sha256 = (Get-Sha256 $workerStdoutPath) } } else { $null }
                stderr = if ($workerStderrPath -and (Test-Path -LiteralPath $workerStderrPath -PathType Leaf)) { [ordered]@{ path = $workerStderrPath; bytes = (Get-Item -LiteralPath $workerStderrPath).Length; sha256 = (Get-Sha256 $workerStderrPath) } } else { $null }
            }
            elapsed_seconds = [math]::Round(((Get-Date) - $started).TotalSeconds, 2)
        })
    }
    exit 2
} finally {
    if (Test-ProcessAlive $workerProcess) {
        Stop-ExactProcessTree $workerProcess.Id
    }
    if ($temporaryPdf -and (Test-Path -LiteralPath $temporaryPdf -PathType Leaf)) {
        Remove-Item -LiteralPath $temporaryPdf -Force -ErrorAction SilentlyContinue
    }
}
