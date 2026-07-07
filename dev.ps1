#!/usr/bin/env pwsh
# MyNestra dev task runner (Windows-first). See `Makefile` for POSIX parity.
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Command = "help",
    [Parameter(Position = 1, ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = "Stop"

switch ($Command) {
    "up"             { docker compose up -d @Rest }
    "down"           { docker compose down @Rest }
    "build"          { docker compose build @Rest }
    "logs"           { docker compose logs -f @Rest }
    "migrate"        { docker compose exec web python manage.py migrate_schemas @Rest }
    "makemigrations" { docker compose exec web python manage.py makemigrations @Rest }
    "test"           { docker compose exec web pytest @Rest }
    "shell"          { docker compose exec web python manage.py shell @Rest }
    "manage"         { docker compose exec web python manage.py @Rest }
    "tailwind"       { docker compose logs -f tailwind }
    "psql"           { docker compose exec db psql -U mynestra -d mynestra @Rest }
    default {
        Write-Host "MyNestra dev - usage: ./dev.ps1 <command> [args]"
        Write-Host ""
        Write-Host "  up              start the stack (detached)"
        Write-Host "  down            stop the stack"
        Write-Host "  build           build images"
        Write-Host "  logs            follow all logs"
        Write-Host "  migrate         migrate_schemas (pass --shared / --tenant / --schema=...)"
        Write-Host "  makemigrations  makemigrations"
        Write-Host "  test            run pytest in the web container"
        Write-Host "  shell           Django shell"
        Write-Host "  manage          arbitrary manage.py command (e.g. ./dev.ps1 manage createsuperuser)"
        Write-Host "  tailwind        follow the Tailwind watcher logs"
        Write-Host "  psql            psql into the db"
    }
}
