<#
Daily data + patrol entry (Windows Task Scheduler, workdays 19:00)
  0) prefetch:   python -m stock prefetch --workers 8      (the ONE fetch point; 0 token)
  1) invest_data: run.py --capital 50000                  (reads the prefetched cache; 0 token)
  2) prune: python -m stock prune --days 90               (keep ~3 months of run data, never touch ledgers)

One data folder: everything fetched tonight lands in data\shared\cache (datasource layout),
and data\invest\cache is a junction to it (see setup_shared_cache.ps1), so the skill reads the
shared copy as its own cache and the unified record of what was fetched is
data\shared\snapshots\<date>\manifest.json.

Division of labour (do not blur it):
- This script FETCHES DATA ONLY. It never computes or writes any ledger.
- The ledger lives with the skill and is settled by the 20:00 DSH patrol session:
    invest: D:\skills\invest\ledger\state_50k.json (patrol.py --date auto)
- Saturday 20:00 decision day uses the weekdays' fetched data; it does not re-fetch here.

Design:
- Only trigger + sequential run + logging + exit code. No change to the skill's fetch logic.
- Step 0 is the single fetch point (stock prefetch); step 1 is pure computation on a hot cache.
- Do NOT pass --refresh to run.py: it sets A_SHARE_FORCE_REFRESH=1 for the whole child chain and
  would bypass the shared cache (that is exactly the old double-fetch/timeout failure mode).
- The skill uses A_SHARE_WORKDIR, so its out/paper state stays isolated; only cache/ is shared
  through the junction.
- invest workdir = D:\skills\invest\stock\data\invest holds cache/out only; the ledger is outside it (skill folder).
- PYTHONPATH includes D:\skills\invest\stock so the skill (and prefetch/prune) can `import stock`.
  (Do NOT use D:\ : it exposes the D:\skills\invest\stock folder as a namespace package and shadows the real one.)
- 0 token: pure python scripts, no LLM, no session.
- -Root is for rehearsal only: shared cache becomes <Root>\shared, workdir <Root>\invest.
- stdout/stderr per step to disk; summary to last_run.json; one history line appended.
- Every record carries a date: patrol_<yyyyMMdd_HHmmss>.log, <stamp>_<step>.out/err.log,
  summary_<yyyy-MM-dd>.json, archive/<yyyy-MM-dd>/, and dated report/decision copies in the archive.
- -RetentionDays (default 90 ~ 3 months) is used both by the prune step and the archive sweep.
  Shared fetch cache is pruned separately at 7 days (STOCK_SHARED_CACHE_RETENTION_DAYS).
- A failing step does not stop the others (monitoring must run).
- The machine-readable summary_<date>.json is written TWICE: right after prefetch (partial=true,
  invest_data=null) so the 20:00 DSH patrol can act even if invest is slow, then again at the end
  (partial=false, ok = prefetch_ok AND invest_ok). pre-2026-11-16 the record was written only at the
  end, so an invest timeout left the patrol with no handoff file at all.
- -SkipInvest: prefetch + prune only (rehearsal, or an invest step known to be broken tonight).
- -SkipPrefetch: run the skill on whatever cache exists (rehearsal/debug; not for production).
- -SkipPrune: skip the retention step (rehearsal/debug).
  NOTE: every switch must be DECLARED here. Undeclared flags passed after -File are silently
  ignored by Windows PowerShell 5.1 - keep this list honest.

Note: keep this file ASCII-only. Windows PowerShell 5.1 reads BOM-less UTF-8 as ANSI and non-ASCII
comments can break parameter binding.
#>
param(
    [string]$Root = '',
    [string]$Ledger = '50k',
    [int]$StepTimeoutSec = 1800,
    [int]$PrefetchTimeoutSec = 2400,
    [int]$Workers = 8,
    [int]$RetentionDays = 90,
    [switch]$SkipInvest,
    [switch]$SkipPrefetch,
    [switch]$SkipPrune
)

$ErrorActionPreference = 'Continue'
$stockHome = Split-Path -Parent $PSScriptRoot
$py = Join-Path $stockHome '.venv\Scripts\python.exe'
$skillsRoot = 'D:\skills'


if ($Root) {
    $investWork = Join-Path $Root 'invest'
    $sharedRoot = Join-Path $Root 'shared'
    $logDir = Join-Path $Root '_logs'
} else {
    $investWork = Join-Path $stockHome 'data\invest'
    $sharedRoot = Join-Path $stockHome 'data\shared'
    $logDir = Join-Path $stockHome 'data\logs'
}
$sharedCache = Join-Path $sharedRoot 'cache'
$outDir = if ($Root) { Join-Path $Root 'out' } else { Join-Path $stockHome 'data\out' }
New-Item -ItemType Directory -Force -Path $logDir, $investWork, $sharedCache, $outDir | Out-Null
# Notes emitted before Write-Log is defined below; flushed right after the function exists.
$bootNotes = @()
$cacheMode = 'junction'
# Production uses a REAL junction created once by setup_shared_cache.ps1. For rehearsal (-Root) build
# the same shape inside the throwaway root so the run behaves like production.
if ($Root) {
    $lc = Join-Path $investWork 'cache'
    if (-not (Test-Path -LiteralPath $lc)) {
        try {
            New-Item -ItemType Junction -Path $lc -Target $sharedCache -ErrorAction Stop | Out-Null
        } catch {
            New-Item -ItemType Directory -Force -Path $lc | Out-Null
            $cacheMode = 'copy'
            $bootNotes += ("WARN junction {0} -> {1} failed: {2}; using a plain directory (cache NOT shared)" -f $lc, $sharedCache, $_.Exception.Message)
        }
    }
} else {
    # In production the skill cache path must resolve to the shared cache. If the junction is missing the
    # run still works but the skill fetches its own copy (the old double-fetch failure) - say it loudly.
    $lc = Join-Path $investWork 'cache'
    $isJunction = $false
    if (Test-Path -LiteralPath $lc) {
        try { $isJunction = ((Get-Item -LiteralPath $lc -Force).LinkType -eq 'Junction') } catch { $isJunction = $false }
    }
    if (-not $isJunction) {
        $cacheMode = 'unshared'
        $bootNotes += ("WARN {0} is not a junction to {1}; the skill will fetch its own copy (run scripts\setup_shared_cache.ps1 -Execute)" -f $lc, $sharedCache)
    }
}

$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$day = Get-Date -Format 'yyyy-MM-dd'
$runLog = Join-Path $logDir "patrol_$stamp.log"
$lastRun = Join-Path $logDir 'last_run.json'
$history = Join-Path $logDir 'history.log'

$env:PYTHONIOENCODING = 'utf-8'
$env:TQDM_DISABLE = '1'
# Make 'import stock' work for the skill and the prefetch/prune steps; pin STOCK_HOME to the repo root.
# Prepend (not overwrite): a rehearsal can put a sitecustomize/netguard dir on PYTHONPATH and the
# scheduled chain must not silently drop it.
if ($env:PYTHONPATH) { $env:PYTHONPATH = "$stockHome;$env:PYTHONPATH" } else { $env:PYTHONPATH = $stockHome }
$env:STOCK_HOME = $stockHome
# stock.cache provenance / OUT_DIR must follow the run root too (a -Root rehearsal must never write
# D:\skills\invest\stock\data\out; stock/config.py reads OUT_DIR from STOCK_OUT_DIR).
$env:STOCK_OUT_DIR = $outDir
if ($Root) {
    # rehearsal: never touch production data\shared
    $env:STOCK_SHARED_DIR = $sharedRoot
    $env:STOCK_CACHE_DIR = (Join-Path $Root 'cache')
    $env:STOCK_INVEST_DIR = $investWork
    $env:STOCK_LOGS_DIR = $logDir
    $env:STOCK_ARCHIVE_DIR = (Join-Path $Root 'archive')
}

if ($env:DSH_PATROL_DEBUG) {
    Write-Host ("DBG root=[{0}] ledger=[{1}] bound=[{2}]" -f $Root, $Ledger, ($PSBoundParameters.Keys -join ','))
}

function Write-Log([string]$text) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $text
    Add-Content -Path $runLog -Value $line -Encoding utf8
    Write-Host $line
}

# junction / cache-mode notes collected before Write-Log existed
foreach ($note in $bootNotes) { Write-Log $note }

function Write-JsonNoBom([string]$Path, $Obj) {
    # PowerShell 5.1 Set-Content -Encoding UTF8 writes a BOM, and json.load() on the reader side
    # rejects it (the 20:00 patrol reads this file). Write BOM-less UTF-8 explicitly.
    $json = $Obj | ConvertTo-Json -Depth 6
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $json, $utf8)
}

function Get-LogTail([string]$path, [int]$n) {
    if (-not (Test-Path $path)) { return '' }
    try { return ((Get-Content -Path $path -Tail $n -Encoding UTF8) -join "`n") } catch { return '' }
}

function Invoke-Step {
    param(
        [string]$Name,
        [string]$WorkDir,
        [string]$EnvName,
        [string]$EnvValue,
        [string]$Exe,
        [string[]]$StepArgs,
        [int]$TimeoutSec = 0
    )
    if ($TimeoutSec -le 0) { $TimeoutSec = $StepTimeoutSec }
    $outFile = Join-Path $logDir ("{0}_{1}.out.log" -f $stamp, $Name)
    $errFile = Join-Path $logDir ("{0}_{1}.err.log" -f $stamp, $Name)
    Set-Item -Path ("Env:" + $EnvName) -Value $EnvValue
    Write-Log ("START {0}  workdir={1}  {2}={3}" -f $Name, $WorkDir, $EnvName, $EnvValue)
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $rc = -1
    $outText = ''
    $errText = ''
    $proc = $null
    try {
        $argLine = ($StepArgs | ForEach-Object {
            if ([string]$_ -match '\s') { '"' + $_ + '"' } else { [string]$_ }
        }) -join ' '
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $Exe
        $psi.Arguments = $argLine
        $psi.WorkingDirectory = $WorkDir
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
        $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8
        $proc = New-Object System.Diagnostics.Process
        $proc.StartInfo = $psi
        [void]$proc.Start()
        $outTask = $proc.StandardOutput.ReadToEndAsync()
        $errTask = $proc.StandardError.ReadToEndAsync()
        if (-not $proc.WaitForExit($TimeoutSec * 1000)) {
            # Kill the whole process tree: children inherit the redirected stdout/stderr pipes,
            # so killing only the direct child makes $outTask.Result block forever (observed 2026-11-16).
            try { & taskkill.exe /PID $proc.Id /T /F 2>$null | Out-Null } catch { }
            try { if (-not $proc.HasExited) { $proc.Kill() } } catch { }
            $rc = -2
            Write-Log ("TIMEOUT {0} after {1}s (tree killed)" -f $Name, $TimeoutSec)
        } else {
            $rc = [int]$proc.ExitCode
        }
        # Bounded read: never let a stray descendant holding the pipe hang the whole scheduled task.
        try { if ($outTask.Wait(15000)) { $outText = $outTask.Result } } catch { $outText = '' }
        try { if ($errTask.Wait(15000)) { $errText = $errTask.Result } } catch { $errText = '' }
    } catch {
        $rc = -3
        Write-Log ("EXCEPTION {0}: {1}" -f $Name, $_.Exception.Message)
    } finally {
        # Process handles are not finalized deterministically under powershell.exe; dispose explicitly
        # (a scheduled task runs this up to 4 times a night and leaks handles otherwise).
        if ($proc) { try { $proc.Dispose() } catch { } }
    }
    $hdr = "[{0}] step={1} rc={2}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Name, $rc
    Set-Content -Path $outFile -Value (@($hdr, '--- stdout ---', $outText) -join "`n") -Encoding UTF8
    Set-Content -Path $errFile -Value (@($hdr, '--- stderr ---', $errText) -join "`n") -Encoding UTF8
    $sw.Stop()
    $secs = [math]::Round($sw.Elapsed.TotalSeconds, 1)
    foreach ($f in @($outFile, $errFile)) {
        if (Test-Path $f) {
            $content = Get-Content -Path $f -Raw -Encoding UTF8
            if ($content -and -not [string]::IsNullOrWhiteSpace($content)) {
                Write-Log ("--- {0} output ({1}) ---" -f $Name, (Split-Path $f -Leaf))
                Add-Content -Path $runLog -Value $content -Encoding utf8
            }
        }
    }
    Write-Log ("END {0}  rc={1}  {2}s" -f $Name, $rc, $secs)
    return [pscustomobject]@{
        name = $Name; workdir = $WorkDir; rc = $rc; seconds = $secs
        stdout = $outFile; stderr = $errFile; env = @{ $EnvName = $EnvValue }
    }
}

$started = Get-Date
Write-Log ("===== daily patrol START  root='{0}'  ledger={1} =====" -f $Root, $Ledger)
Write-Log ("cache_mode={0}  shared_cache={1}  out_dir={2}" -f $cacheMode, $sharedCache, $outDir)

# Worst-case wall budget of the steps below. The scheduled task's ExecutionTimeLimit must be >= this
# (register_task.ps1 defaults to 3h); with the old 2h limit a slow-night timeout could kill the run
# mid-invest, which is exactly what happened on 2026-11-16.
$budgetSec = 0
if (-not $SkipPrefetch) { $budgetSec += $PrefetchTimeoutSec }
if (-not $SkipInvest) { $budgetSec += $StepTimeoutSec }
$taskTimeLimitSec = 10800
if ($budgetSec -gt $taskTimeLimitSec) {
    Write-Log ("WARN worst-case step budget {0}s exceeds the task time limit {1}s; enlarge the limit or lower the per-step timeouts" -f $budgetSec, $taskTimeLimitSec)
} else {
    Write-Log ("step budget {0}s <= task time limit {1}s (ok)" -f $budgetSec, $taskTimeLimitSec)
}

$steps = @()

# ---------- step 0: the ONE fetch point -> data\shared\cache + snapshots\<date>\manifest.json ----------
$prefetchStep = $null
if (-not $SkipPrefetch) {
    $prefetchStep = Invoke-Step -Name 'prefetch' -WorkDir $stockHome -EnvName 'STOCK_HOME' -EnvValue $stockHome -Exe $py -StepArgs @('-m', 'stock', 'prefetch', '--workers', "$Workers") -TimeoutSec $PrefetchTimeoutSec
    $steps += $prefetchStep
} else {
    Write-Log 'SKIP prefetch (-SkipPrefetch)'
}
$manifestPath = Join-Path $sharedRoot ('snapshots\' + $day + '\manifest.json')
$manifestDoc = $null
if (Test-Path $manifestPath) {
    try { $manifestDoc = Get-Content -Path $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json } catch { $manifestDoc = $null }
}
$prefetchOk = if ($prefetchStep) { ($prefetchStep.rc -eq 0) } else { $null }
# Exit-code contract (stock prefetch): 0 ok, 1 step error, 2 coverage/bar failed, 3 intraday blocked,
# -2 timeout from Invoke-Step. Keep the machine-readable reason in the handoff record.
$prefetchReason = $null
if ($prefetchStep) {
    if ($prefetchStep.rc -eq 3) { $prefetchReason = 'blocked_intraday' }
    elseif ($prefetchStep.rc -eq 2) { $prefetchReason = 'coverage_or_bar_failed' }
    elseif ($prefetchStep.rc -eq -2) { $prefetchReason = 'timeout' }
    elseif ($prefetchStep.rc -ne 0) { $prefetchReason = 'error' }
    if ($prefetchReason) { Write-Log ("WARN prefetch rc={0} reason={1}" -f $prefetchStep.rc, $prefetchReason) }
}
$prefetchData = [pscustomobject]@{
    rc = if ($prefetchStep) { $prefetchStep.rc } else { $null }
    reason = $prefetchReason
    seconds = if ($prefetchStep) { $prefetchStep.seconds } else { $null }
    shared_cache = $sharedCache
    manifest = $manifestPath
    manifest_exists = (Test-Path $manifestPath)
    data_date = if ($manifestDoc) { $manifestDoc.data_date } else { $null }
    data_bar_date = if ($manifestDoc) { $manifestDoc.data_bar_date } else { $null }
    expected_trade_date = if ($manifestDoc) { $manifestDoc.expected_trade_date } else { $null }
    bar_ok = if ($manifestDoc) { $manifestDoc.bar_ok } else { $null }
    coverage_ok = if ($manifestDoc) { $manifestDoc.coverage.ok } else { $null }
    items = if ($manifestDoc) { $manifestDoc.totals.items } else { $null }
    failed = if ($manifestDoc) { $manifestDoc.totals.failed } else { $null }
    from_cache = if ($manifestDoc) { $manifestDoc.totals.from_cache } else { $null }
    error_tail = if ($prefetchStep) { Get-LogTail $prefetchStep.stderr 5 } else { '' }
}

# ---------- handoff record: expose fetched data immediately, never wait for invest ----------
$runId = $stamp
$summaryFile = Join-Path $logDir ("summary_$day.json")

function Write-Handoff([bool]$Partial, $InvestOk, $InvestData) {
    $rec = [pscustomobject]@{
        date = $day
        at = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
        run_id = $runId
        partial = $Partial
        ok = ((-not $Partial) -and (($null -eq $prefetchOk) -or ($prefetchOk -eq $true)) -and (($null -eq $InvestOk) -or ($InvestOk -eq $true)))
        prefetch_ok = $prefetchOk
        invest_ok = $InvestOk
        ledger = $Ledger
        ledger_settled_by = 'DSH 20:00 patrol sessions (invest patrol.py); this script never computes ledgers'
        data_cache = $sharedCache
        prefetch_data = $prefetchData
        invest_data = $InvestData
    }
    Write-JsonNoBom -Path $summaryFile -Obj $rec
}

Write-Handoff -Partial $true -InvestOk $null -InvestData $null
Write-Log ("handoff: partial summary written ({0}) prefetch_ok={1}" -f $summaryFile, $prefetchOk)

if (-not $SkipInvest) {
    # NO --refresh here: it would set A_SHARE_FORCE_REFRESH=1 for the child chain and re-fetch
    # everything, defeating the shared cache (the old timeout bug). The cache is already hot.
    $steps += Invoke-Step -Name 'invest_data' -WorkDir (Join-Path $skillsRoot 'invest\scripts') -EnvName 'A_SHARE_WORKDIR' -EnvValue $investWork -Exe $py -StepArgs @('run.py', '--capital', '50000')
} else {
    Write-Log 'SKIP invest_data (-SkipInvest)'
}
# Retention (~90 days) for run data. Ledgers/credentials are never pruned; prune failure does not fail the run.
# Rehearsal mode (-Root) never prunes real data.
if (-not $Root -and -not $SkipPrune) {
    $steps += Invoke-Step -Name 'prune' -WorkDir $stockHome -EnvName 'STOCK_HOME' -EnvValue $stockHome -Exe $py -StepArgs @('-m', 'stock', 'prune', '--days', "$RetentionDays")
} elseif ($SkipPrune) {
    Write-Log 'SKIP prune (-SkipPrune)'
}

$finished = Get-Date
$ok = (@($steps | Where-Object { $_.rc -ne 0 -and $_.name -ne 'prune' }).Count -eq 0)
$summary = [pscustomobject]@{
    run_id = $runId
    started_at = $started.ToString('yyyy-MM-dd HH:mm:ss')
    finished_at = $finished.ToString('yyyy-MM-dd HH:mm:ss')
    elapsed_sec = [math]::Round(($finished - $started).TotalSeconds, 1)
    root = $Root
    ledger = $Ledger
    retention_days = $RetentionDays
    cache_mode = $cacheMode
    step_budget_sec = $budgetSec
    ok = $ok
    prefetch = $prefetchData
    steps = $steps
    log = $runLog
}
Write-JsonNoBom -Path $lastRun -Obj $summary
Copy-Item -Path $runLog -Destination (Join-Path $logDir 'latest.log') -Force

# ---------- final handoff: the fetch record was already written right after prefetch ----------
$dataStep = $steps | Where-Object { $_.name -eq 'invest_data' } | Select-Object -First 1
$dataInfo = $null
if ($dataStep) {
    $planJson = Join-Path $investWork 'out\stage3_plan.json'
    $planDoc = $null
    if (Test-Path $planJson) {
        try { $planDoc = Get-Content -Path $planJson -Raw -Encoding UTF8 | ConvertFrom-Json } catch { $planDoc = $null }
    }
    $dataInfo = [pscustomobject]@{
        rc = $dataStep.rc
        seconds = $dataStep.seconds
        data_as_of = if ($planDoc) { $planDoc.data_as_of } else { $null }
        plan_generated_at = if ($planDoc) { $planDoc.generated_at } else { $null }
        capital = if ($planDoc) { $planDoc.capital } else { $null }
        error_tail = Get-LogTail $dataStep.stderr 5
    }
}
$investOk = if ($dataStep) { ($dataStep.rc -eq 0) } else { $null }
Write-Handoff -Partial $false -InvestOk $investOk -InvestData $dataInfo
Write-Log ("handoff: final summary written partial=false invest_ok={0}" -f $investOk)

# ---------- per-day archive: archive/<YYYY-MM-DD>/ (production) ----------
$archiveRoot = Join-Path (Split-Path $logDir -Parent) 'archive'
$archiveDir = Join-Path $archiveRoot $day
New-Item -ItemType Directory -Force -Path $archiveDir | Out-Null
$archiveStale = New-Object System.Collections.ArrayList
$archiveMissing = New-Object System.Collections.ArrayList

function Copy-IfExists([string]$src, [string]$dstName, [int]$MaxAgeDays = 0) {
    if (-not (Test-Path $src)) {
        [void]$archiveMissing.Add($dstName)
        return $false
    }
    if ($MaxAgeDays -gt 0) {
        # A "latest" file that stopped being produced (invest writes data_manifest.json only on DSH
        # decision days) must not be archived as if it were today's artifact.
        try {
            $ageDays = ((Get-Date) - (Get-Item -LiteralPath $src).LastWriteTime).TotalDays
            if ($ageDays -gt $MaxAgeDays) {
                [void]$archiveStale.Add(("{0} ({1:n1}d old)" -f $dstName, $ageDays))
                Write-Log ("WARN archive skip stale {0}: {1:n1} days old (> {2}d)" -f $dstName, $ageDays, $MaxAgeDays)
                return $false
            }
        } catch { }
    }
    Copy-Item -Path $src -Destination (Join-Path $archiveDir $dstName) -Force
    return $true
}

# unified fetch record for tonight (what was fetched into data\shared\cache and whether it covered everything)
[void](Copy-IfExists $manifestPath 'shared_prefetch_manifest.json')
[void](Copy-IfExists (Join-Path $sharedRoot ('snapshots\' + $day + '\summary.json')) 'shared_prefetch_summary.json')
[void](Copy-IfExists (Join-Path $sharedRoot 'latest.json') 'shared_latest.json')
# invest data artifacts produced by this run. The ledger is never copied here: it is settled by DSH.
[void](Copy-IfExists (Join-Path $investWork 'out\stage1_funnel.json') 'invest_stage1_funnel.json')
[void](Copy-IfExists (Join-Path $investWork 'out\stage1_candidates.csv') 'invest_stage1_candidates.csv')
[void](Copy-IfExists (Join-Path $investWork 'out\stage2_judged.csv') 'invest_stage2_judged.csv')
[void](Copy-IfExists (Join-Path $investWork 'out\stage3_plan.json') 'invest_stage3_plan.json')
[void](Copy-IfExists (Join-Path $investWork 'out\stage3_plan.md') 'invest_stage3_plan.md')
[void](Copy-IfExists (Join-Path $investWork 'out\stage3_orders.csv') 'invest_stage3_orders.csv')
[void](Copy-IfExists (Join-Path $investWork 'out\data_snapshot.csv') 'invest_data_snapshot.csv')
# produced only by the DSH decision-day handoff (team.py); run.py does not write it -> 2-day freshness gate.
[void](Copy-IfExists (Join-Path $investWork 'out\data_manifest.json') 'invest_data_manifest.json' 2)
[void](Copy-IfExists (Join-Path $investWork 'daily_log.md') 'invest_daily_log.md')
[void](Copy-IfExists (Join-Path $investWork 'cash_rates_live.json') 'invest_cash_rates_live.json')
# dated copies of the latest decision / report / weekly (produced by the DSH sessions; self-contained records)
$invDate = $day
[void](Copy-IfExists (Join-Path $investWork 'out\paper\decision_latest.json') 'invest_decision_latest.json' 7)
[void](Copy-IfExists (Join-Path $investWork ('out\paper\report_' + $invDate + '.html')) ('invest_report_' + $invDate + '.html'))
[void](Copy-IfExists (Join-Path $investWork ('out\paper\weekly_' + $invDate + '.md')) ('invest_weekly_' + $invDate + '.md'))
[void](Copy-IfExists (Join-Path $investWork ('out\paper\orders_' + $invDate + '.csv')) ('invest_orders_' + $invDate + '.csv'))
[void](Copy-IfExists $summaryFile 'summary.json')
[void](Copy-IfExists $runLog ('patrol_' + $stamp + '.log'))
[void](Copy-IfExists (Join-Path $logDir 'latest.log') 'latest.log')

# Record what the archive could not copy: stale (exists but too old to be tonight's) vs missing (absent).
$archiveInfo = [pscustomobject]@{
    dir = $archiveDir
    stale = @($archiveStale)
    missing = @($archiveMissing)
}
Write-JsonNoBom -Path (Join-Path $archiveDir '_archive_notes.json') -Obj $archiveInfo
$summary | Add-Member -NotePropertyName archive -NotePropertyValue $archiveInfo -Force
# last_run is copied last so the archived copy carries the archive notes too.
Write-JsonNoBom -Path $lastRun -Obj $summary
[void](Copy-IfExists $lastRun 'last_run.json')
if (@($archiveStale).Count -or @($archiveMissing).Count) {
    Write-Log ("archive notes: stale={0} missing={1} (see _archive_notes.json)" -f @($archiveStale).Count, @($archiveMissing).Count)
}

# retention: keep $RetentionDays days of archives (belt & braces; the prune step above does the same)
try {
    $cut = (Get-Date).AddDays(-$RetentionDays)
    Get-ChildItem -Path $archiveRoot -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt $cut } |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
} catch { }

$dataRc = if ($dataStep) { $dataStep.rc } else { 'n/a' }
$preRc = if ($prefetchStep) { $prefetchStep.rc } else { 'n/a' }
$line = "{0} ok={1} elapsed={2}s prefetch_rc={3} invest_data_rc={4} log={5}" -f `
    $started.ToString('yyyy-MM-dd HH:mm:ss'), $ok, $summary.elapsed_sec,
    $preRc, $dataRc, (Split-Path $runLog -Leaf)
Add-Content -Path $history -Value $line -Encoding utf8

Write-Log ("summary: {0}" -f $summaryFile)
Write-Log ("archive: {0}" -f $archiveDir)
Write-Log ("===== daily patrol END  ok={0}  elapsed={1}s =====" -f $ok, $summary.elapsed_sec)
if ($ok) { exit 0 } else { exit 1 }
