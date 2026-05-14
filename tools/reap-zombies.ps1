<#
.SYNOPSIS
Find and kill orphaned symbiosis-brain MCP processes left over from closed Claude Code sessions.

.DESCRIPTION
Walks the parent chain of every process matching 'symbiosis_brain' or 'symbiosis-brain'.
A process is considered an orphan if no live 'claude.exe' ancestor is found within 20 PPID hops.
Default mode shows the candidates and asks for [Y/N] confirmation. Use -DryRun to only inspect.
Use -Force to skip the prompt.

.EXAMPLE
.\tools\reap-zombies.ps1
Interactive mode: lists orphans, asks Y/N, kills on confirm.

.EXAMPLE
.\tools\reap-zombies.ps1 -DryRun
Inspect only — show candidates without killing.

.EXAMPLE
.\tools\reap-zombies.ps1 -Force
Kill all detected orphans without prompting (use in automation).
#>
param(
    [switch]$DryRun,
    [switch]$Force
)

# NOTE: Process name match uses -like 'claude*' to survive future Anthropic
# renames (claude.exe → claude-code.exe etc). Update the pattern here if a
# new executable name ships and isn't covered.
function Test-HasLiveClaudeAncestor {
    param([int]$ProcessId)
    $cur = $ProcessId
    $depth = 0
    while ($cur -and $depth -lt 20) {
        $p = Get-CimInstance Win32_Process -Filter "ProcessId=$cur" -ErrorAction SilentlyContinue
        if (-not $p) { return $false }
        if ($p.Name -like 'claude*') { return $true }
        if ($p.ParentProcessId -le 0 -or $p.ParentProcessId -eq $cur) { return $false }
        $cur = $p.ParentProcessId
        $depth++
    }
    return $false
}

Write-Host "Scanning for orphaned symbiosis-brain processes..."

$candidates = Get-CimInstance Win32_Process |
    Where-Object {
        $_.CommandLine -match 'symbiosis[_-]brain' -and
        ($_.Name -like 'python*' -or $_.Name -like 'symbiosis-brain*')
    } |
    Where-Object { -not (Test-HasLiveClaudeAncestor $_.ProcessId) }

if (-not $candidates) {
    Write-Host "OK - no orphans found."
    exit 0
}

$totalMB = [math]::Round(($candidates | Measure-Object WorkingSetSize -Sum).Sum / 1MB, 1)
Write-Host ""
Write-Host "Found $($candidates.Count) orphan(s), $totalMB MB reclaimable:"
$candidates | Format-Table -AutoSize -Wrap @(
    'ProcessId',
    'ParentProcessId',
    @{ Name = 'Started'; Expression = { $_.CreationDate } },
    @{ Name = 'WS_MB'; Expression = { [math]::Round($_.WorkingSetSize / 1MB, 1) } },
    'Name'
)

if ($DryRun) {
    Write-Host "(dry-run; nothing killed)"
    exit 0
}

if (-not $Force) {
    $reply = Read-Host "Kill all? [Y/N]"
    if ($reply -notmatch '^[yY]') {
        Write-Host "Aborted."
        exit 0
    }
}

$killed = 0
foreach ($c in $candidates) {
    try {
        Stop-Process -Id $c.ProcessId -Force -ErrorAction Stop
        $killed++
    } catch {
        Write-Warning "Failed to kill PID $($c.ProcessId): $_"
    }
}
Write-Host "Killed $killed/$($candidates.Count) process(es). Reclaimed approximately $totalMB MB."
