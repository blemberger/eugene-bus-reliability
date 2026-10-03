# Run `make` on its own for the list of commands. `make setup` first; host commands
# (load-static, poll-once, report, app, test, dbt-*) need the venv: source .venv/bin/activate
-include .env
export

.DEFAULT_GOAL := help

DBT := DBT_PROFILES_DIR=dbt dbt
HOST_PGPORT := $(or $(POSTGRES_PORT),5432)
TUNNEL_PORT := $(or $(TUNNEL_PORT),55439)
VPS_DIR := $(or $(VPS_DIR),/opt/eugene-bus-reliability)

.PHONY: help setup lock up down reset logs load-static poll-once poll scheduler run migrate check-db replay rebuild report dump server-dump deploy fetch-dump app tunnel test test-all lint format dbt-deps dbt-build dbt-docs

help:             ## list these commands
	@grep -hE '^[a-z-]+:.*## ' $(firstword $(MAKEFILE_LIST)) | awk 'BEGIN {FS = ":.*## "} {printf "  %-12s %s\n", $$1, $$2}'

setup:            ## create .env if missing, and a venv with the dev and app dependencies
	@test -f .env || cp .env.example .env
	python3 -m venv .venv && . .venv/bin/activate && pip install -c constraints.txt -e ".[dev,app]"

lock:             ## re-pin constraints.txt to what is installed in .venv (after a deliberate upgrade)
	@{ echo "# Exact versions of every dependency, known to work together. Regenerate with: make lock"; \
	  .venv/bin/pip freeze --exclude-editable; } > constraints.txt
	@echo "wrote constraints.txt"

up:               ## start Postgres and the throwaway test database (schema is applied on first start)
	docker compose up -d --wait db db_test

down:             ## stop every container, dashboard included (data is kept)
	docker compose --profile web down

reset:            ## stop every container AND delete the database volumes (re-applies sql/schema on next `up`)
	docker compose --profile web down -v

logs:             ## follow the poller and scheduler logs (Ctrl+C stops watching, not them)
	docker compose logs -f --tail=100 poller scheduler

load-static:      ## download the current LTD schedule and load it if new
	python -m ltdwatch load-static

poll-once:        ## proof of principle: 10 cycles in the foreground, then a report
	python -m ltdwatch poll --cycles 10
	python -m ltdwatch report

poll:             ## run the poller continuously in the background (docker)
	docker compose up -d --build poller

scheduler:        ## run the dbt build (every 15 min) and daily maintenance in the background (docker)
	docker compose up -d --build scheduler

run:              ## everything that should run unattended: db, poller, scheduler
	docker compose up -d --build db poller scheduler

check-db:         ## can the owner and the read-only user both log in?
	@docker compose exec -T db psql "postgresql://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@localhost/$(POSTGRES_DB)" -qtAc "select 1" >/dev/null 2>&1 && echo "owner:  ok" || echo "owner:  FAILED (POSTGRES_PASSWORD in .env does not match the database)"
	@docker compose exec -T db psql "postgresql://ltd_reader:$(READER_PASSWORD)@localhost/$(POSTGRES_DB)" -qtAc "select 1" >/dev/null 2>&1 && echo "reader: ok" || echo "reader: FAILED (run make migrate; it sets ltd_reader's password from READER_PASSWORD)"

migrate:          ## apply sql/migrations/*.sql (if any) to both databases, then set the read-only user's password
	@set -e; for f in sql/migrations/*.sql; do \
	  [ -e "$$f" ] || continue; \
	  echo "applying $$f"; \
	  docker compose exec -T db psql -U $(POSTGRES_USER) -d $(POSTGRES_DB) -v ON_ERROR_STOP=1 -q -f - < "$$f"; \
	  if [ -n "$$(docker compose ps -q --status running db_test)" ]; then \
	    docker compose exec -T db_test psql -U $(POSTGRES_USER) -d $(POSTGRES_DB) -v ON_ERROR_STOP=1 -q -f - < "$$f"; \
	  fi; \
	done
	@echo "alter role ltd_reader password :'pw';" | docker compose exec -T db psql -U $(POSTGRES_USER) -d $(POSTGRES_DB) -v ON_ERROR_STOP=1 -q -v pw="$(READER_PASSWORD)"
	@echo "ltd_reader password set from READER_PASSWORD"

replay:           ## store archived feed messages that are missing from the database (idempotent)
	docker compose run --rm --build poller python -m ltdwatch replay

rebuild:          ## EMPTY the realtime tables and rebuild them from the raw archive (poller paused meanwhile)
	docker compose stop poller
	docker compose run --rm --build poller python -m ltdwatch replay --rebuild; status=$$?; docker compose start poller; exit $$status

report:           ## what has been collected so far
	python -m ltdwatch report

dump:             ## everything needed to diagnose a problem, in dump.txt: pipeline, clocks, every app page; RAW=1 adds raw feed samples
	@echo "collecting pipeline state, then rendering every app page (about a minute)..."
	@python -m ltdwatch dump $(if $(RAW),--raw) > dump.txt 2>&1 || echo "!! dump exited with an error (details above)" >> dump.txt
	@python app/selfcheck.py >> dump.txt 2>&1 || echo "!! app self-check exited with an error (details above)" >> dump.txt
	@echo "wrote $(CURDIR)/dump.txt ($$(wc -l < dump.txt) lines)"

server-dump:      ## on the server (no venv there): the same dump, run inside the containers, plus memory, disk and log errors, into dump.txt
	@echo "collecting pipeline state, then rendering every app page (a minute or two)..."
	@{ docker compose exec -T poller python -m ltdwatch dump $(if $(RAW),--raw) || echo "!! dump exited with an error (details above)"; \
	  echo; echo "== SERVER: load, memory, disk, containers"; uptime; free -h; df -h /; du -sh data/raw; \
	  docker compose --profile web ps; docker stats --no-stream; \
	  echo; echo "== LOG LINES with ERROR/WARNING/Traceback/denied (last 300 lines of each service)"; \
	  docker compose --profile web logs --no-color --tail 300 poller scheduler app caddy \
	    | grep -E "ERROR|WARN|Traceback|denied|FATAL" | grep -vE "Done\. PASS=[0-9]+ WARN=0 ERROR=0" | tail -40; \
	  echo; docker compose --profile web exec -T app python app/selfcheck.py || echo "!! app self-check exited with an error (details above)"; \
	} > dump.txt 2>&1
	@echo "wrote $(CURDIR)/dump.txt ($$(wc -l < dump.txt) lines)"

deploy:           ## from the laptop: bring the server up to date with GitHub and restart what changed (needs VPS_HOST in .env)
	@test -n "$(VPS_HOST)" || { echo "add VPS_HOST=root@<server ip> to .env first"; exit 1; }
	@test -z "$$(git status --porcelain)" || { echo "you have unsaved changes here: commit and push them first"; exit 1; }
	@git fetch -q && test "$$(git rev-parse HEAD)" = "$$(git rev-parse @{u})" || { echo "this laptop and GitHub differ: git push (or git pull) first"; exit 1; }
	ssh $(VPS_HOST) 'cd $(VPS_DIR) && git pull --ff-only && docker compose --profile web up -d --build db poller scheduler app caddy && docker compose --profile web exec -T caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile && docker compose --profile web ps'

fetch-dump:       ## from the laptop: make the server's dump and copy it here as dump.txt (needs VPS_HOST in .env)
	@test -n "$(VPS_HOST)" || { echo "add VPS_HOST=root@<server ip> to .env first"; exit 1; }
	ssh $(VPS_HOST) 'cd $(VPS_DIR) && make server-dump'
	scp -q $(VPS_HOST):$(VPS_DIR)/dump.txt dump.txt
	@echo "copied the server's dump to $(CURDIR)/dump.txt ($$(wc -l < dump.txt) lines)"

app:              ## local dashboard at http://localhost:8501 (Ctrl+C to stop)
	streamlit run app/streamlit_app.py

tunnel:           ## forward the server's database to localhost:55439 (leave running; see docs/deploy.md)
	ssh -N -L $(TUNNEL_PORT):127.0.0.1:$(HOST_PGPORT) $(VPS_HOST)

test:             ## unit tests only (no database)
	pytest -q -m "not integration"

test-all:         ## unit + integration tests (needs `make up`; uses TEST_DATABASE_URL, never DATABASE_URL)
	pytest -q

lint:             ## the same checks CI runs: lint rules and formatting
	ruff check . && ruff format --check .

format:           ## apply the safe lint fixes and the standard formatting
	ruff check --fix . && ruff format .

dbt-deps:         ## install dbt's package (dbt_utils) into dbt/dbt_packages
	$(DBT) deps --project-dir dbt

dbt-build:        ## build the analysis layer (observed arrivals, reliability marts) and run its tests
	PGHOST=localhost PGPORT=$(HOST_PGPORT) $(DBT) build --project-dir dbt

dbt-docs:         ## generate and serve dbt's documentation site with the lineage graph
	PGHOST=localhost PGPORT=$(HOST_PGPORT) $(DBT) docs generate --project-dir dbt && $(DBT) docs serve --project-dir dbt