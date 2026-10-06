function Get-OkkiPerformanceBudget {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [ValidateRange(8, 4096)]
        [int]$TotalMemoryGB,

        [Parameter(Mandatory = $true)]
        [ValidateRange(1, 4096)]
        [int]$LogicalProcessors,

        [ValidateRange(1, 8)]
        [int]$InstanceCount = 4
    )

    # Budget all planned collectors as one workload. Reserving 25% of RAM for
    # Chromium, Electron, the OS and filesystem cache prevents four independent
    # processes from each assuming that the whole workstation belongs to it.
    $ReserveGB = [math]::Max(8, [math]::Ceiling($TotalMemoryGB * 0.25))
    $ReserveGB = [math]::Min($ReserveGB, [math]::Max(4, $TotalMemoryGB - 8))
    $UsableGB = [math]::Max(8, $TotalMemoryGB - $ReserveGB)
    $TotalHeapGB = [math]::Max(4 * $InstanceCount, [math]::Floor($UsableGB * 0.80))
    $TotalHeapGB = [math]::Min(160, $TotalHeapGB)
    $HeapPerInstanceMB = [math]::Floor(($TotalHeapGB * 1024) / $InstanceCount)
    $HeapPerInstanceMB = [math]::Min(65536, [math]::Max(4096, $HeapPerInstanceMB))

    $TotalApiConcurrency = [math]::Min(64, [math]::Max(24, [math]::Floor($LogicalProcessors / 2)))
    $TotalMailConcurrency = [math]::Min(48, [math]::Max(16, [math]::Floor($LogicalProcessors / 3)))
    $TotalResourceConcurrency = if ($TotalMemoryGB -ge 192) { 20 } elseif ($TotalMemoryGB -ge 96) { 12 } else { 8 }
    $TotalCaseScanConcurrency = [math]::Min(128, [math]::Max(32, [math]::Floor($LogicalProcessors * 2 / 3)))
    $TotalUvThreads = [math]::Min(64, [math]::Max(16, [math]::Floor($LogicalProcessors / 2)))

    [pscustomobject]@{
        InstanceCount = $InstanceCount
        ReservedMemoryGB = $ReserveGB
        TotalNodeHeapMB = $HeapPerInstanceMB * $InstanceCount
        NodeHeapPerInstanceMB = $HeapPerInstanceMB
        ApiPageConcurrency = [math]::Max(4, [math]::Floor($TotalApiConcurrency / $InstanceCount))
        MailDetailConcurrency = [math]::Max(4, [math]::Floor($TotalMailConcurrency / $InstanceCount))
        ResourceDownloadConcurrency = [math]::Max(2, [math]::Floor($TotalResourceConcurrency / $InstanceCount))
        CaseScanConcurrency = [math]::Max(8, [math]::Floor($TotalCaseScanConcurrency / $InstanceCount))
        UvThreadpoolSize = [math]::Max(4, [math]::Floor($TotalUvThreads / $InstanceCount))
    }
}
