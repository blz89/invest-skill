<#
One-time (idempotent) migration: move the skill cache into ONE shared cache
and leave a junction behind, so the skill reads/writes the shared folder.

    data\shared\cache\<kind>\<key>__<date>.csv     <- the one copy (datasource cache layout)
    data\invest\cache  --junction--> data\shared\cache

Why: the skill stores its data through stock\skilldata\datasource.py under its own
WORKDIR\cache. The nightly fetch fills data\shared\cache, and the skill reads it as its
own cache; no skill code changes.

Behaviour
- Default is a DRY RUN (prints the plan). Add -Execute to actually migrate.
- Merge rule per file: destination missing -> move; destination exists -> newer mtime wins
  (the older copy is deleted). Negative-cache markers (*.failed) are NOT migrated.
- The old folder is renamed to <cache>.bak-<yyyyMMdd_HHmmss> before the junction is created,
  so nothing is destroyed; delete the .bak folder yourself once you are happy.
- Safe to re-run: an existing junction is simply re-pointed/verified.
- Junctions on the same volume need no admin rights.

Usage
    powershell -NoProfile -ExecutionPolicy Bypass -File <本技能目录>\stock\scripts\setup_shared_cache.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File <本技能目录>\stock\scripts\setup_shared_cache.ps1 -Execute
    ... -Execute -SharedDir <本技能目录>\stock\data\shared -SkipInvest   (no-op guard)
    powershell -NoProfile -ExecutionPolicy Bypass -File <本技能目录>\stock\scripts\setup_shared_cache.ps1 -Rollback
        (undo: delete the junction and rename the newest <cache>.bak-* back into place)
#>
param(
    [string]$StockHome = '',
    [string]$SharedDir = '',
    [switch]$Execute,
    [switch]$SkipInvest,
    [switch]$Rollback
)

$ErrorActionPreference = 'Stop'

if (-not $StockHome) { $StockHome = Split-Path -Parent $PSScriptRoot }

if (-not $SharedDir) { $SharedDir = Join-Path $StockHome 'data\shared' }
$sharedCache = Join-Path $SharedDir 'cache'
$sharedSnapshots = Join-Path $SharedDir 'snapshots'

$targets = @()
if (-not $SkipInvest) { $targets += (Join-Path $StockHome 'data\invest\cache') }
if ($targets.Count -eq 0) { throw 'nothing to do: -SkipInvest was given' }

$mode = if ($Execute) { 'EXECUTE' } else { 'DRY-RUN' }
Write-Host ("[{0}] stock shared-cache setup" -f $mode)
Write-Host ("  shared cache    : {0}" -f $sharedCache)
Write-Host ("  shared snapshots: {0}" -f $sharedSnapshots)
foreach ($t in $targets) { Write-Host ("  merge source    : {0}" -f $t) }

# ------------------------------------------------------------------ helpers
function Test-IsJunction([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    try {
        $it = Get-Item -LiteralPath $Path -Force
        return ($null -ne $it.LinkType)
    } catch { return $false }
}

function Get-JunctionTarget([string]$Path) {
    try {
        $it = Get-Item -LiteralPath $Path -Force
        if ($it.LinkType -and $it.Target) { return [string]($it.Target | Select-Object -First 1) }
    } catch { }
    return ''
}

function Merge-CacheDir([string]$Src, [string]$Dst, [bool]$DoIt) {
    $stats = [pscustomobject]@{
        src = $Src; moved = 0; kept_newer = 0; skipped_failed = 0; bytes = 0L
    }
    if (-not (Test-Path -LiteralPath $Src)) { return $stats }
    if (Test-IsJunction $Src) { return $stats }   # already migrated
    $srcFull = (Resolve-Path -LiteralPath $Src).ProviderPath
    $files = Get-ChildItem -LiteralPath $srcFull -Recurse -File -Force -ErrorAction SilentlyContinue
    foreach ($f in $files) {
        $rel = $f.FullName.Substring($srcFull.Length).TrimStart('\')
        if ($f.Name -like '*.failed') {
            $stats.skipped_failed++
            if ($DoIt) { Remove-Item -LiteralPath $f.FullName -Force -ErrorAction SilentlyContinue }
            continue
        }
        $dest = Join-Path $Dst $rel
        if (-not (Test-Path -LiteralPath $dest)) {
            $stats.moved++
            $stats.bytes += $f.Length
            if ($DoIt) {
                New-Item -ItemType Directory -Force -Path (Split-Path $dest -Parent) | Out-Null
                Move-Item -LiteralPath $f.FullName -Destination $dest -Force
            }
            continue
        }
        $d = Get-Item -LiteralPath $dest -Force
        if ($f.LastWriteTimeUtc -gt $d.LastWriteTimeUtc) {
            $stats.moved++
            $stats.bytes += $f.Length
            if ($DoIt) { Move-Item -LiteralPath $f.FullName -Destination $dest -Force }
        } else {
            # keep the newer destination copy; drop the stale source file
            $stats.kept_newer++
            if ($DoIt) { Remove-Item -LiteralPath $f.FullName -Force -ErrorAction SilentlyContinue }
        }
    }
    if ($DoIt) {
        # remove now-empty source subdirectories (leaves the root itself)
        Get-ChildItem -LiteralPath $srcFull -Recurse -Directory -Force -ErrorAction SilentlyContinue |
            Sort-Object { $_.FullName.Length } -Descending |
            ForEach-Object {
                try { if (-not (Get-ChildItem -LiteralPath $_.FullName -Force | Select-Object -First 1)) { Remove-Item -LiteralPath $_.FullName -Force } } catch { }
            }
    }
    return $stats
}

function Set-SharedJunction([string]$Link, [string]$Target, [bool]$DoIt) {
    if (-not $DoIt) { return }
    $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    $backup = $null
    if (Test-Path -LiteralPath $Link) {
        $bak = "$Link.bak-$stamp"
        Rename-Item -LiteralPath $Link -NewName (Split-Path $bak -Leaf)
        $backup = $bak
        Write-Host ("  renamed old cache -> {0}" -f $bak)
    }
    $parent = Split-Path $Link -Parent
    if ($parent -and -not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }
    try {
        New-Item -ItemType Junction -Path $Link -Target $Target -ErrorAction Stop | Out-Null
    } catch {
        # Roll back: without the junction the skill has no cache directory at all (it would silently
        # start writing a private copy), so restore the original folder before failing loudly.
        if ($backup) {
            try { Rename-Item -LiteralPath $backup -NewName (Split-Path $Link -Leaf) } catch { }
            Write-Host ("  rolled back: {0} restored from {1}" -f $Link, $backup)
        }
        throw ("junction creation failed for {0}: {1}" -f $Link, $_.Exception.Message)
    }
    if ($backup) { Write-Host ("  backup kept: {0}" -f $backup) }
}

function Remove-JunctionOnly([string]$Link) {
    # Remove-Item -Recurse on a junction can follow into the target on PowerShell 5.1; use the .NET
    # API that deletes the reparse point itself (and never its target contents).
    try { [System.IO.Directory]::Delete($Link, $false) } catch { Remove-Item -LiteralPath $Link -Force -ErrorAction SilentlyContinue }
}

# ------------------------------------------------------------------ rollback
if ($Rollback) {
    foreach ($src in $targets) {
        if (-not (Test-IsJunction $src)) {
            Write-Host ("[rollback] {0} is not a junction, nothing to undo" -f $src)
            continue
        }
        $bakDir = Split-Path $src -Parent
        $leaf = Split-Path $src -Leaf
        $bak = Get-ChildItem -LiteralPath $bakDir -Directory -Force -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like ("{0}.bak-*" -f $leaf) } |
            Sort-Object Name -Descending | Select-Object -First 1
        Remove-JunctionOnly $src
        if ($bak) {
            Rename-Item -LiteralPath $bak.FullName -NewName $leaf
            Write-Host ("[rollback] {0} -> restored {1}" -f $src, $bak.Name)
        } else {
            New-Item -ItemType Directory -Force -Path $src | Out-Null
            Write-Host ("[rollback] {0} -> nothing to restore, recreated an empty dir" -f $src)
        }
    }
    Write-Host ''
    Write-Host 'Rollback done. The skill is back on its own cache directory.'
    Write-Host 'NOTE: files already merged into data\shared\cache are NOT moved back (the restored dir starts empty).'
    exit 0
}

# ------------------------------------------------------------------ migrate
# Only the EXECUTE path may create directories: a dry run used to leave data\shared\cache and
# shared_probe files behind, which surprised the operator and polluted the cache root.
if ($Execute) {
    New-Item -ItemType Directory -Force -Path $sharedCache, $sharedSnapshots | Out-Null
    Get-ChildItem -LiteralPath $sharedCache -Filter '.shared_probe_*' -Force -ErrorAction SilentlyContinue |
        Remove-Item -Force -ErrorAction SilentlyContinue
}

$all = @()
foreach ($src in $targets) {
    $linked = Test-IsJunction $src
    if ($linked) {
        $cur = Get-JunctionTarget $src
        $curReal = ''
        try { if ($cur) { $curReal = (Resolve-Path -LiteralPath $cur).ProviderPath } } catch { }
        $wantReal = ''
        if (Test-Path -LiteralPath $sharedCache) { $wantReal = (Resolve-Path -LiteralPath $sharedCache).ProviderPath }
        Write-Host ("[link] {0} is already a junction -> {1}" -f $src, $cur)
        if ($curReal -eq $wantReal) { Write-Host '       target is correct, nothing to do'; continue }
    }
    $st = Merge-CacheDir -Src $src -Dst $sharedCache -DoIt ([bool]$Execute)
    $all += $st
    Write-Host ("[merge] {0}: move {1} | keep-newer {2} | skip .failed {3} | {4:N1} MB" -f `
        $src, $st.moved, $st.kept_newer, $st.skipped_failed, ($st.bytes / 1MB))
    Set-SharedJunction -Link $src -Target $sharedCache -DoIt ([bool]$Execute)
    if ($Execute) {
        $t = Get-JunctionTarget $src
        if ($t -and ((Resolve-Path -LiteralPath $t).ProviderPath -ne (Resolve-Path -LiteralPath $sharedCache).ProviderPath)) {
            throw ("junction check failed: {0} -> {1}" -f $src, $t)
        }
        Write-Host ("[link] {0} -> {1}" -f $src, (Get-JunctionTarget $src))
    }
}

# ------------------------------------------------------------------ verify
if ($Execute) {
    $probeName = ('.shared_probe_{0}_{1}' -f 11720, (Get-Date -Format 'HHmmss'))
    $probe = Join-Path $targets[0] $probeName
    $mirror = Join-Path $sharedCache $probeName
    try {
        Set-Content -LiteralPath $probe -Value 'ok' -Encoding ASCII
        if (-not (Test-Path -LiteralPath $mirror)) { throw 'write-through probe failed: file not visible in shared cache' }
        Write-Host ('[verify] write-through OK: {0} visible in shared cache' -f $probeName)
    } finally {
        Remove-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $mirror -Force -ErrorAction SilentlyContinue
    }
    $nShared = (Get-ChildItem -LiteralPath $sharedCache -Recurse -File -Force -ErrorAction SilentlyContinue).Count
    Write-Host ("[verify] shared cache now holds {0} files" -f $nShared)
    Write-Host ''
    Write-Host 'Next: nightly job now runs  python -m stock prefetch  before the skill.'
    Write-Host '      Keep the .bak folders for a few days, then delete them.'
} else {
    Write-Host ''
    Write-Host 'DRY-RUN only. Re-run with -Execute to migrate.'
}
