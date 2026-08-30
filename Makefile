# ontoMeta 本地开发常用命令（Batch B0 / B2 / B3）
# 用法：在仓库根目录执行 make <target>

.PHONY: help install install-backend install-frontend backend frontend health migrate test smoke compose-up compose-down start stop restart status \
		orch-preflight orch-up-airflow orch-up-warehouse orch-up-all orch-down orch-logs

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
	@echo "  make smoke             端到端冒烟：一张表跑到目标仓里真的有数据"
	@echo "  make health            检查 GET /health"
	@echo "  make start             一键启动前后端（后台运行）"
	@echo "  make stop              一键停止前后端"
	@echo "  make restart           一键重启前后端"
	@echo "  make status            查看服务状态"
	@echo "  make compose-up        docker compose up --build -d"
	@echo "  make compose-down      docker compose down"
	@echo "  --- 物化编排验证栈 ---"
	@echo "  make orch-preflight    起栈前检查（镜像/网络/端口/已有服务）"
	@echo "  make orch-up-airflow   起 Airflow（含 DataHub 血缘插件）"
	@echo "  make orch-up-warehouse 起 Doris（目标数仓）"
	@echo "  make orch-down         停整个编排栈"

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
		ONTOMETA_ADMIN_TOKEN="$${ONTOMETA_ADMIN_TOKEN:-dev-admin-token-change-me}" \
		uvicorn app.main:app --reload --reload-dir app --host 0.0.0.0 --port 8000

frontend:
	cd "$(FRONTEND)" && npm run dev

migrate:
	cd "$(BACKEND)" && . .venv/bin/activate && alembic upgrade head

test:
	cd "$(BACKEND)" && . .venv/bin/activate && pytest -q

# 端到端冒烟：一张表，从本体一路走到目标仓里真的有数据（见 scripts/smoke.py 的说明）。
# 需要后端起着（make backend）以及 Airflow / 目标仓可达。换表：SMOKE_ENTITY=item make smoke
smoke:
	@cd "$(BACKEND)" && . .venv/bin/activate && python "$(ROOT)scripts/smoke.py"

health:
	@curl -sf http://127.0.0.1:8000/health && echo || \
		(echo "health check failed: is backend running on :8000?" >&2; exit 1)

start:
	@bash "$(ROOT)service.sh" start

stop:
	@bash "$(ROOT)service.sh" stop

restart:
	@bash "$(ROOT)service.sh" restart

status:
	@bash "$(ROOT)service.sh" status

compose-up:
	docker compose up --build -d

compose-down:
	docker compose down

# ---- 物化编排验证栈（Airflow + Flink + 可选 Doris），见 docker/orchestration/README.md ----
ORCH := docker compose -f $(ROOT)docker/orchestration/docker-compose.yml

orch-preflight:
	@bash "$(ROOT)docker/orchestration/preflight.sh"

orch-up-airflow: orch-preflight
	$(ORCH) --profile airflow up -d --build

orch-up-warehouse:
	$(ORCH) --profile warehouse up -d

orch-up-all: orch-preflight
	$(ORCH) --profile all up -d --build

orch-down:
	$(ORCH) --profile all down

orch-logs:
	$(ORCH) --profile all logs -f --tail=100
