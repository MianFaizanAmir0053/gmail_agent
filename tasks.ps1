<#
Task runner. `make` is not installed on this machine, so this replaces the
Makefile referenced in the module plans -- `make eval` becomes `.\tasks.ps1 eval`.

Usage:  .\tasks.ps1 <task> [-EnvFile <settings file>] [args...]
Run it from the project's folder, as uv is run.
#>
param(
    [Parameter(Position = 0)]
    [ValidateSet('setup', 'lint', 'fmt', 'typecheck', 'test', 'check', 'run', 'up', 'down',
        'eval', 'reauth', 'smoke', 'fernet', 'migrate', 'models',
        'poll', 'approve', 'telegram', 'serve', 'agent', 'report', 'measure', 'reprice', 'vapid',
        'ingest', 'search', 'retrieval-eval', 'publish', 'pause', 'resume', 'status',
        'injection-eval', 'planner-eval', 'mail-sync')]
    [string]$Task = 'check',

    # A settings file, read as `uv run --env-file` reads one: a variable
    # already set keeps its value. On the laptop, production's, from the
    # ..\mailagent-prod worktree (docs/DEPLOY.md, section 12):
    #   .\tasks.ps1 approve --list -EnvFile C:/Users/Faizan/Desktop/hobby/gmail_agent/secrets/prod.env
    [string]$EnvFile = '',

    # Extra args forwarded to the underlying command, e.g.
    #   .\tasks.ps1 eval --extractor always_yes --no-save
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest = @()
)

$ErrorActionPreference = 'Stop'

# --- Python ------------------------------------------------------------
# Windows' Smart App Control can block .venv\Scripts\python.exe, a launcher
# with no reputation of its own, while the Python uv installed still runs.
# The launcher only starts that Python with __PYVENV_LAUNCHER__ naming
# itself, and Python then runs as the venv; multiprocessing starts its
# workers the same way (bpo-35797). So when the launcher is blocked,
# Invoke-Venv does what it would. `uv run`, `uv sync` and `uv lock` run the
# launcher first, so the lock's packages then go in with `uv pip`, and uv
# checks the lock with its own Python, against an environment that does
# not exist.

$venv = Join-Path (Get-Location).ProviderPath '.venv'
$venvPython = Join-Path $venv 'Scripts\python.exe'
$script:blocked = $null  # found out on first use
$script:synced = $false

function Test-LauncherBlocked {
    if (-not (Test-Path -LiteralPath $venvPython)) { return $false }  # `uv run` makes one
    try {
        & $venvPython -c 'pass'
        return $false
    } catch [System.Management.Automation.ApplicationFailedException] {
        # 4551: "An Application Control policy has blocked this file."
        if ($_.Exception.InnerException.NativeErrorCode -eq 4551) { return $true }
        throw
    }
}

function Get-BasePython {
    # Where the launcher looks: pyvenv.cfg's `home`.
    $line = Get-Content -LiteralPath (Join-Path $venv 'pyvenv.cfg') |
        Where-Object { $_ -match '^\s*home\s*=' } | Select-Object -First 1
    if (-not $line) { throw "$venv\pyvenv.cfg names no home." }
    Join-Path ($line -replace '^\s*home\s*=\s*', '').Trim() 'python.exe'
}

function Invoke-WithEnv([System.Collections.IDictionary]$Vars, [scriptblock]$Body) {
    # Process variables for $Body alone. What was there before is put back,
    # so the shell that ran this never keeps production's settings.
    $saved = @{}
    foreach ($name in $Vars.Keys) {
        $saved[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
        [Environment]::SetEnvironmentVariable($name, $Vars[$name], 'Process')
    }
    try { & $Body } finally {
        foreach ($name in $saved.Keys) {
            [Environment]::SetEnvironmentVariable($name, $saved[$name], 'Process')
        }
    }
}

function Read-EnvFile([string]$Path) {
    # Plain KEY=value lines only. A line uv would read its own way -- quotes,
    # a `$`, `#` or backslash in the value, or no value -- stops the run
    # rather than being guessed at. An error names the line, never the value.
    $vars = [ordered]@{}
    $number = 0
    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $number++
        $line = $line.Trim()
        if (-not $line -or $line.StartsWith('#')) { continue }
        if ($line -notmatch '^([A-Za-z_][A-Za-z0-9_]*)=([^\s"''`$#\\]+)$') {
            throw "${Path}, line ${number}: only a plain KEY=value line can be read without uv."
        }
        $vars[$Matches[1]] = $Matches[2]
    }
    $vars
}

function Sync-Venv([string]$BasePython) {
    # What `uv run` does first: the venv brought into line with uv.lock. A
    # package is added or changed, never removed.
    $temp = [IO.Path]::GetTempPath()
    $lock = Join-Path $temp "mailagent-lock-$PID.txt"
    Invoke-WithEnv @{ UV_PROJECT_ENVIRONMENT = (Join-Path $temp "mailagent-no-env-$PID") } {
        uv export --locked --no-emit-project --format requirements-txt --quiet --output-file $lock
    }
    if ($LASTEXITCODE) { throw 'uv could not export uv.lock: see above.' }
    try {
        # Each line carries its hashes, and uv checks them unasked. Not
        # --require-hashes: it would also want [tool.uv]'s constraint pinned.
        uv pip install --python $BasePython --prefix $venv --no-deps --quiet -r $lock
        if ($LASTEXITCODE) { throw 'uv could not install the packages uv.lock names: see above.' }
    } finally {
        Remove-Item -LiteralPath $lock -ErrorAction SilentlyContinue
    }
}

function Invoke-Venv {
    # Python in the project's venv, with these args: `uv run python ...`, or,
    # while Windows blocks the launcher, uv's Python started as it would be.
    $pyArgs = $args
    if ($null -eq $script:blocked) {
        $script:blocked = Test-LauncherBlocked
        if ($script:blocked) {
            Write-Host "Windows blocks .venv\Scripts\python.exe, so uv's own Python runs as the venv." -ForegroundColor DarkYellow
        }
    }
    if (-not $script:blocked) {
        if ($EnvFile) { uv run --env-file $EnvFile python @pyArgs } else { uv run python @pyArgs }
        return
    }
    $vars = [ordered]@{}
    if ($EnvFile) {
        foreach ($entry in (Read-EnvFile $EnvFile).GetEnumerator()) {
            if ($null -eq [Environment]::GetEnvironmentVariable($entry.Key, 'Process')) {
                $vars[$entry.Key] = $entry.Value
            }
        }
    }
    $base = Get-BasePython
    if (-not $script:synced) {
        Sync-Venv $base
        $script:synced = $true
    }
    $vars['__PYVENV_LAUNCHER__'] = $venvPython
    Invoke-WithEnv $vars { & $base @pyArgs }
}

switch ($Task) {
    'setup' {
        uv python install 3.12
        if (Test-LauncherBlocked) { Sync-Venv (Get-BasePython) } else { uv sync }
        if (-not (Test-Path .env)) {
            Copy-Item .env.example .env
            Write-Host "Created .env from .env.example -- fill in the required values." -ForegroundColor Yellow
        }
    }
    'lint'      { Invoke-Venv -m ruff check . }
    'fmt'       { Invoke-Venv -m ruff format . ; Invoke-Venv -m ruff check --fix . }
    'typecheck' { Invoke-Venv -m mypy app tests }
    'test'      { Invoke-Venv -m pytest }
    'check' {
        Invoke-Venv -m ruff check .
        Invoke-Venv -m ruff format --check .
        Invoke-Venv -m mypy app tests
        Invoke-Venv -m pytest
    }
    'run'  { Invoke-Venv -m app.main }
    'up'   { docker compose up -d db }
    'down' { docker compose down }
    'eval' { Invoke-Venv -m app.eval.run @Rest }
    # The injection suite's model half (M18, D8): five samples a case, on the
    # development key. `--baseline` writes the run CI checks.
    'injection-eval' { Invoke-Venv -m app.eval.injection @Rest }
    # The tool-routing eval (M19, D10): five samples a case, on the
    # development key. `--baseline` writes the run CI checks.
    'planner-eval' { Invoke-Venv -m app.eval.planner @Rest }

    'migrate' { Invoke-Venv -m app.store.db }
    'models'  { Invoke-Venv -m app.extraction.models @Rest }

    # --- M06 -------------------------------------------------------------
    'poll'     { Invoke-Venv -m app.jobs.poll @Rest }
    'approve'  { Invoke-Venv -m app.jobs.approve @Rest }
    # The owner's switches (M17, D6), against the database `.env` names: each
    # prints that database's host. On Fly:
    # fly ssh console -C "sh -c 'cd /app && python -m app.jobs.control pause'"
    'pause'    { Invoke-Venv -m app.jobs.control pause }
    'resume'   { Invoke-Venv -m app.jobs.control resume }
    'status'   { Invoke-Venv -m app.jobs.control status }
    'telegram' { Invoke-Venv -m app.jobs.telegram_bot @Rest }
    'report'   { Invoke-Venv -m app.jobs.report @Rest }
    'measure'  { Invoke-Venv -m app.jobs.measure @Rest }
    'reprice'  { Invoke-Venv -m app.jobs.reprice @Rest }
    'vapid'    { Invoke-Venv -m app.jobs.vapid }
    # Mail sync's command line (M20): --status, --show, --once and the rest
    # (docs/DEPLOY.md, section 10.5).
    'mail-sync' { Invoke-Venv -m app.mail.sync @Rest }

    # Retrieval (M10-M12)
    'ingest'         { Invoke-Venv -m app.jobs.ingest_job @Rest }
    'search'         { Invoke-Venv -m app.rag.search @Rest }
    'retrieval-eval' { Invoke-Venv -m app.eval.retrieval @Rest }
    'publish'        { Invoke-Venv -m app.eval.publish @Rest }
    'serve'    { Invoke-Venv -m uvicorn app.api:app --reload --port 8000 }
    # The backend as production runs it, without reload. On the laptop,
    # secrets\start-prod.ps1 runs it in ..\mailagent-prod with -EnvFile.
    'agent'    { Invoke-Venv -m uvicorn app.api:app --host 127.0.0.1 --port 8000 }

    # --- M01 -------------------------------------------------------------
    'fernet' { Invoke-Venv -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" }
    'reauth' { Invoke-Venv -m app.google.reauth @Rest }
    'smoke'  { Invoke-Venv -m app.google.smoke }
}
