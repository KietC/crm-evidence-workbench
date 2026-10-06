# Start-Process joins ArgumentList into one native command line; quote paths explicitly.
# Start-Process 会把 ArgumentList 拼成原生命令行；包含空格的路径必须明确加引号。
function ConvertTo-NativePathArgument {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)
    if ($Path.Contains('"')) { throw 'Native path argument must not contain a quote.' }
    return '"' + $Path + '"'
}
