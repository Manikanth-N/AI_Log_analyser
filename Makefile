.PHONY: install dev-up dev-down models db frontend test lint

install:
	pip install -e ".[dev]"

dev-up:
	docker compose up -d postgres redis qdrant

dev-down:
	docker compose down

models:
	bash scripts/setup_ollama.sh

db:
	python scripts/init_db.py

api:
	uvicorn api.main:app --reload --port 8000

worker-parse:
	celery -A api.workers.celery_app worker -Q parse -c 2 -n parse@%h

worker-investigate:
	celery -A api.workers.celery_app worker -Q investigate -c 1 -n investigate@%h

frontend:
	cd frontend && npm install && npm run dev

test:
	pytest tests/unit/

test-integration:
	pytest tests/integration/ -m integration

lint:
	ruff check . && mypy --ignore-missing-imports api/ agents/ pipeline/ intelligence/ parsers/ storage/ llm/ orchestrator/

format:
	ruff format .
