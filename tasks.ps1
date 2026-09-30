<#
Task runner. `make` is not installed on this machine, so this replaces the
Makefile referenced in the module plans -- `make eval` becomes `.\tasks.ps1 eval`.

Usage:  .\tasks.ps1 <task>
#>
param(
    [Parameter(Position = 0)]
    [ValidateSet('setup', 'lint', 'fmt', 'typecheck', 'test', 'check', 'run', 'up', 'down',
        'eval', 'reauth', 'smoke', 'fernet', 'migrate', 'models',
        'poll', 'approve', 'telegram', 'serve', 'report', 'measure', 'reprice',
        'ingest', 'search', 'retrieval-eval', 'publish')]
    [string]$Task = 'check',

    # Extra args forwarded to the underlying command, e.g.
    #   .\tasks.ps1 eval --extractor always_yes --no-save
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest = @()
)

$ErrorActionPreference = 'Stop'

switch ($Task) {
    'setup' {
        uv python install 3.12
        uv sync
        if (-not (Test-Path .env)) {
            Copy-Item .env.example .env
            Write-Host "Created .env from .env.example -- fill in the required values." -ForegroundColor Yellow
        }
    }
    'lint'      { uv run ruff check . }
    'fmt'       { uv run ruff format . ; uv run ruff check --fix . }
    'typecheck' { uv run mypy app tests }
    'test'      { uv run pytest }
    'check' {
        uv run ruff check .
        uv run ruff format --check .
        uv run mypy app tests
        uv run pytest
    }
    'run'  { uv run python -m app.main }
    'up'   { docker compose up -d db }
    'down' { docker compose down }
    'eval' { uv run python -m app.eval.run @Rest }

    'migrate' { uv run python -m app.store.db }
    'models'  { uv run python -m app.extraction.models @Rest }

    # --- M06 -------------------------------------------------------------
    'poll'     { uv run python -m app.jobs.poll @Rest }
    'approve'  { uv run python -m app.jobs.approve @Rest }
    'telegram' { uv run python -m app.jobs.telegram_bot @Rest }
    'report'   { uv run python -m app.jobs.report @Rest }
    'measure'  { uv run python -m app.jobs.measure @Rest }
    'reprice'  { uv run python -m app.jobs.reprice @Rest }

    # Retrieval (M10-M12)
    'ingest'         { uv run python -m app.jobs.ingest_job @Rest }
    'search'         { uv run python -m app.rag.search @Rest }
    'retrieval-eval' { uv run python -m app.eval.retrieval @Rest }
    'publish'        { uv run python -m app.eval.publish @Rest }
    'serve'    { uv run uvicorn app.api:app --reload --port 8000 }

    # --- M01 -------------------------------------------------------------
    'fernet' { uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" }
    'reauth' { uv run python -m app.google.reauth @Rest }
    'smoke'  { uv run python -m app.google.smoke }
}
