# Day-to-day commands for running the bot with Docker Compose.
# `make` alone lists the targets.

COMPOSE ?= docker compose
SERVICE ?= bot
BACKUP  ?= bot-data.tgz

.DEFAULT_GOAL := help
.PHONY: help data build rebuild up down restart update logs ps check login backup restore test lint

help: ## Show this help
	@awk 'BEGIN {FS = ":.*## "} /^[a-z-]+:.*## / {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

data: ## Create ./data and make it writable for the container user (UID 10001)
	@mkdir -p data
	$(COMPOSE) run --rm --no-deps --user 0 --cap-add CHOWN --entrypoint chown $(SERVICE) 10001:10001 /data

build: ## Build the image (uses the layer cache)
	$(COMPOSE) build

rebuild: ## Rebuild the image from scratch with a fresh base image
	$(COMPOSE) build --pull --no-cache

up: data ## Start the stack in the background
	$(COMPOSE) up -d

down: ## Stop and remove the containers (./data is kept)
	$(COMPOSE) down

restart: build data ## Rebuild the image and recreate the stack
	$(COMPOSE) up -d --force-recreate --remove-orphans

update: ## Pull the latest code, rebuild and restart
	git pull --ff-only
	$(MAKE) restart

logs: ## Follow the bot logs
	$(COMPOSE) logs -f --tail=100 $(SERVICE)

ps: ## Show container status and health
	$(COMPOSE) ps

check: data ## Poll the station once and show what the detector decides
	$(COMPOSE) run --rm $(SERVICE) check

login: data ## Sign in to BLUETTI interactively and store the tokens
	$(COMPOSE) run --rm $(SERVICE) login

backup: ## Stop the bot, archive ./data to bot-data.tgz (BACKUP=path), start again
	$(COMPOSE) stop $(SERVICE)
	$(COMPOSE) run --rm -T --no-deps --entrypoint tar $(SERVICE) czf - -C /data . > $(BACKUP)
	chmod 600 $(BACKUP)
	$(COMPOSE) start $(SERVICE)
	@echo "Saved $(BACKUP) - it contains tokens, keep it private."

restore: data ## Replace ./data with bot-data.tgz (BACKUP=path), start the bot
	@test -f $(BACKUP) || { echo "$(BACKUP) not found"; exit 1; }
	$(COMPOSE) stop $(SERVICE)
	$(COMPOSE) run --rm -T --no-deps --entrypoint sh $(SERVICE) \
		-c 'find /data -mindepth 1 -delete && tar xzf - -C /data' < $(BACKUP)
	$(COMPOSE) up -d

test: ## Run the test suite
	uv run pytest

lint: ## Run ruff and mypy
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy
