# MyNestra dev task runner (POSIX parity with dev.ps1).
.PHONY: help up down build logs migrate makemigrations test shell tailwind psql

help:
	@echo "MyNestra dev - make <target>"
	@echo "  up down build logs migrate makemigrations test shell tailwind psql"

up:
	docker compose up -d

down:
	docker compose down

build:
	docker compose build

logs:
	docker compose logs -f

migrate:
	docker compose exec web python manage.py migrate_schemas

makemigrations:
	docker compose exec web python manage.py makemigrations

test:
	docker compose exec web pytest

shell:
	docker compose exec web python manage.py shell

tailwind:
	docker compose logs -f tailwind

psql:
	docker compose exec db psql -U mynestra -d mynestra
