dev-build: ## Build development Docker image
	docker-compose up -d --build api-dev

dev-down: ## Stop development container
	docker-compose down api-dev

dev-up:
	docker-compose up

# connect to the db container
db-connect:
	docker compose exec -it db psql -U eventify -d eventify

# run database seeds
db-seed:
	docker compose exec -T db psql -U eventify -d eventify < app/db/seed.sql

db-drop:
	docker compose stop api-dev
	docker compose exec -T db psql -U eventify -d postgres -c "SELECT pg_terminate_backend(pg_stat_activity.pid) FROM pg_stat_activity WHERE pg_stat_activity.datname = 'eventify' AND pid <> pg_backend_pid();"
	docker compose exec -T db psql -U eventify -d postgres -c "DROP DATABASE IF EXISTS eventify;"

db-create:
	docker compose exec -T db psql -U eventify -d postgres -c "CREATE DATABASE eventify;"

db-fix-schema:
	docker compose exec -T db psql -U eventify -d eventify -c "ALTER TABLE events ALTER COLUMN type DROP NOT NULL;"

db-reset:
	docker compose exec -T db psql -U eventify -d postgres -c "DROP DATABASE IF EXISTS eventify;"
	docker compose exec -T db psql -U eventify -d postgres -c "CREATE DATABASE eventify;"
	docker compose start api-dev

migrate-create:
	docker compose exec -T api-dev alembic revision --autogenerate -m "$(msg)"

migrate:
	docker compose exec -T api-dev alembic upgrade head

downgrade:
	docker compose exec -T api-dev alembic downgrade -1

seed-admin:
	docker compose exec -T api-dev python -m app.db.seed_admin

db-clear:
	docker compose exec -T db psql -U eventify -d eventify -c "TRUNCATE TABLE events, venues, neighborhoods CASCADE;"
