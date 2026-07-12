.PHONY: help dev dev-api dev-web build test web-install sync docker docker-run

help:
	@echo "targets:"
	@echo "  make sync          install Python deps (uv sync --extra dev)"
	@echo "  make web-install   install frontend deps (npm install)"
	@echo "  make dev           run api + web together; Ctrl+C stops both"
	@echo "  make dev-api       run FastAPI on :8000"
	@echo "  make dev-web       run rsbuild dev server on :5173"
	@echo "  make build         rsbuild production build"
	@echo "  make test          backend pytest suite"
	@echo "  make docker        build the kana-quiz:latest image"
	@echo "  make docker-run    run the image on :8000 with a named volume"

sync:
	uv sync --extra dev

web-install:
	cd frontend && npm install

dev:
	@./scripts/dev.sh

dev-api:
	uv run uvicorn kana_quiz.main:app --reload --port 8000 --app-dir backend

dev-web:
	cd frontend && npm run dev

build:
	cd frontend && npm run build

test:
	uv run --extra dev pytest -q

docker:
	docker build -t kana-quiz:latest .

docker-run:
	docker run --rm -p 8000:8000 -v kana-quiz-data:/data kana-quiz:latest
