# EN: Local-only convert legacy office readonly utility; preserve input evidence and keep source content out of logs.
# 中文：本地辅助工具；保留输入证据，不把源内容写入外部日志。
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Source,
    [Parameter(Mandatory = $true)][string]$OutputDir,
    [switch]$Worker
)

$ErrorActionPreference = 'Stop'
$sourcePath = (Resolve-Path -LiteralPath $Source).Path
$targetRoot = [System.IO.Path]::GetFullPath($OutputDir)
[System.IO.Directory]::CreateDirectory($targetRoot) | Out-Null
$extension = [System.IO.Path]::GetExtension($sourcePath).ToLowerInvariant()

if (-not $Worker) {
    $officeNames = @('WINWORD', 'EXCEL', 'POWERPNT')
    $before = @(Get-Process -Name $officeNames -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id)
    $beforePowerShell = @(Get-Process -Name powershell -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id)
    $job = Start-Job -ScriptBlock {
        param($scriptPath, $inputPath, $targetPath)
        & $scriptPath -Source $inputPath -OutputDir $targetPath -Worker
    } -ArgumentList $PSCommandPath, $sourcePath, $targetRoot
    try {
        # Corrupt legacy files can leave an invisible modal prompt even with
        # alerts disabled.  Thirty seconds is ample for these small case files;
        # the Python layer immediately tries the local LibreOffice fallback.
        $deadline = [DateTimeOffset]::UtcNow.AddSeconds(30)
        while ($job.State -in @('NotStarted', 'Running') -and [DateTimeOffset]::UtcNow -lt $deadline) {
            Start-Sleep -Milliseconds 500
            $job = Get-Job -Id $job.Id
        }
        if ($job.State -in @('NotStarted', 'Running')) {
            Get-Process -Name powershell -ErrorAction SilentlyContinue |
                Where-Object { $_.Id -ne $PID -and $beforePowerShell -notcontains $_.Id } |
                Stop-Process -Force -ErrorAction SilentlyContinue
            throw 'LEGACY_OFFICE_COM_TIMEOUT'
        }
        Receive-Job -Job $job -ErrorAction Stop | Out-Null
        if ($job.State -ne 'Completed') { throw 'LEGACY_OFFICE_COM_CONVERSION_FAILED' }
    }
    finally {
        Remove-Job -Job $job -Force -ErrorAction SilentlyContinue
        Get-Process -Name $officeNames -ErrorAction SilentlyContinue |
            Where-Object { $before -notcontains $_.Id } |
            Stop-Process -Force -ErrorAction SilentlyContinue
    }
    return
}

$application = $null
$document = $null

try {
    switch ($extension) {
        '.doc' {
            $application = New-Object -ComObject Word.Application
            $application.Visible = $false
            $application.DisplayAlerts = 0
            try { $application.AutomationSecurity = 3 } catch {}
            try { $application.Options.UpdateLinksAtOpen = $false } catch {}
            # Supply every prompt-producing option explicitly.  A damaged,
            # password-protected, or conversion-ambiguous legacy document must
            # fail closed instead of leaving an invisible modal dialog behind.
            $document = $application.Documents.OpenNoRepairDialog(
                $sourcePath, # FileName
                $false,      # ConfirmConversions
                $true,       # ReadOnly
                $false,      # AddToRecentFiles
                '',          # PasswordDocument
                '',          # PasswordTemplate
                $true,       # Revert
                '',          # WritePasswordDocument
                '',          # WritePasswordTemplate
                0,           # Format: auto-detect
                0,           # Encoding: auto-detect
                $false,      # Visible
                $false,      # OpenAndRepair
                0,           # DocumentDirection
                $true,       # NoEncodingDialog
                ''           # XMLTransform
            )
            $destination = Join-Path $targetRoot 'converted.docx'
            if (Test-Path -LiteralPath $destination) { Remove-Item -LiteralPath $destination -Force }
            $document.SaveAs2($destination, 16)
        }
        '.xls' {
            $application = New-Object -ComObject Excel.Application
            $application.Visible = $false
            $application.DisplayAlerts = $false
            try { $application.AutomationSecurity = 3 } catch {}
            try { $application.AskToUpdateLinks = $false } catch {}
            $document = $application.Workbooks.Open($sourcePath, 0, $true)
            $destination = Join-Path $targetRoot 'converted.xlsx'
            if (Test-Path -LiteralPath $destination) { Remove-Item -LiteralPath $destination -Force }
            $document.SaveAs($destination, 51)
        }
        '.ppt' {
            $application = New-Object -ComObject PowerPoint.Application
            try { $application.AutomationSecurity = 3 } catch {}
            $document = $application.Presentations.Open($sourcePath, $true, $false, $false)
            $destination = Join-Path $targetRoot 'converted.pptx'
            if (Test-Path -LiteralPath $destination) { Remove-Item -LiteralPath $destination -Force }
            $document.SaveAs($destination, 24)
        }
        default { throw 'LEGACY_OFFICE_EXTENSION_UNSUPPORTED' }
    }
    if (-not (Test-Path -LiteralPath $destination -PathType Leaf)) {
        throw 'LEGACY_OFFICE_CONVERSION_OUTPUT_MISSING'
    }
}
finally {
    if ($null -ne $document) {
        try { $document.Close($false) } catch {}
        try { [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($document) } catch {}
    }
    if ($null -ne $application) {
        try { $application.Quit() } catch {}
        try { [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($application) } catch {}
    }
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
}
