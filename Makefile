# ontoMeta 本地开发常用命令（Batch B0 / B2 / B3）
# 用法：在仓库根目录执行 make <target>

.PHONY: help install install-backend install-frontend backend frontend health migrate test compose-up compose-down

ROOT := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
BACKEND := $(ROOT)backend
FRONTEND := $(ROOT)frontend

help:
	@echo "ontoMeta 开发命令"
	@echo "  make install           安装前后端依赖"
	@echo "  make install-backend   仅安装后端（venv + pip）"
	@echo "  make install-frontend  仅安装前端（npm）"
	@echo "  make backend           启动后端 :8000（需已 install-backend）"
	@echo "  make frontend          启动前端 :5180（需已 install-frontend）"
	@echo "  make migrate           执行 Alembic upgrade head"
	@echo "  make test              运行后端 pytest"
	@echo "  make health            检查 GET /health"
	@echo "  make compose-up        docker compose up --build -d"
	@echo "  make compose-down      docker compose down"

install: install-backend install-frontend

install-backend:
	cd "$(BACKEND)" && \
		(test -d .venv || python3 -m venv .venv) && \
		. .venv/bin/activate && \
		pip install -r requirements.txt && \
		(test -f .env || cp .env.example .env)

install-frontend:
	cd "$(FRONTEND)" && npm install

backend:
	cd "$(BACKEND)" && . .venv/bin/activate && \
		uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

frontend:
	cd "$(FRONTEND)" && npm run dev

migrate:
	cd "$(BACKEND)" && . .venv/bin/activate && alembic upgrade head

test:
	cd "$(BACKEND)" && . .venv/bin/activate && pytest -q

health:
	@curl -sf http://127.0.0.1:8000/health && echo || \
		(echo "health check failed: is backend running on :8000?" >&2; exit 1)

compose-up:
	docker compose up --build -d

compose-down:
	docker compose down
