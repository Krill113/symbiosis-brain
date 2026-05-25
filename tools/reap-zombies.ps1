<#
.SYNOPSIS
Find and kill orphaned processes left over from closed Claude Code sessions.

.DESCRIPTION
Reaps two classes of leftover process:
  1. MCP servers   — python/symbiosis-brain processes whose command line matches 'symbiosis_brain'.
     Orphan = no live 'claude.exe' ancestor within 20 PPID hops.
  2. Statusline hooks — bash.exe processes whose command line matches '.claude/hooks' (e.g.
     sb-statusline.sh, sb-line.sh, statusline.sh). These chain bash → bash through short-lived
     intermediate shells, so the ancestor walk is unreliable here (even a current-session hook
     loses its live claude ancestor). Instead they are judged by AGE: a healthy statusline render
     completes in well under a second, so any such bash alive longer than -StaleMinutes (default 2)
     is stuck/orphaned. This avoids killing the current session's in-flight renders.
Default mode shows the candidates and asks for [Y/N] confirmation. Use -DryRun to only inspect.
Use -Force to skip the prompt. Use -StaleMinutes to tune the bash-hook age threshold.

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
    [switch]$Force,
    [double]$StaleMinutes = 2
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

Write-Host "Scanning for orphaned symbiosis-brain MCP and statusline-hook processes..."

$staleBefore = (Get-Date).AddMinutes(-$StaleMinutes)

$candidates = Get-CimInstance Win32_Process | Where-Object {
    # Class 1: MCP servers — orphan if no live claude ancestor
    if ($_.CommandLine -match 'symbiosis[_-]brain' -and
        ($_.Name -like 'python*' -or $_.Name -like 'symbiosis-brain*')) {
        return -not (Test-HasLiveClaudeAncestor $_.ProcessId)
    }
    # Class 2: statusline bash hooks — orphan if stuck alive past the age threshold
    # (ancestor walk is unreliable for the nested bash chain; a healthy render exits in <1s)
    if ($_.Name -like 'bash*' -and $_.CommandLine -match '[\\/]\.claude[\\/]hooks') {
        return $_.CreationDate -lt $staleBefore
    }
    return $false
}

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
