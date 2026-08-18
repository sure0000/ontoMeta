# ontoMeta 本地开发常用命令（Batch B0 / B2 / B3）
# 用法：在仓库根目录执行 make <target>

.PHONY: help install install-backend install-frontend backend frontend health migrate test dag-parse smoke compose-up compose-down start stop restart status \
	orch-preflight orch-up-airflow orch-up-sync orch-up-warehouse orch-up-all orch-down orch-logs

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
	@echo "  make dag-parse         用真 Airflow 的 DagBag 解析一遍生成的 DAG"
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
	@echo "  make orch-up-sync      起 SeaTunnel"
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

# 用**真 Airflow** 的 DagBag 解析一遍生成的 DAG。单元测试里的 ast.parse 只看语法、桩模块
# 只验我们自己写的那部分逻辑；Operator 关键字合不合法、provider 在不在、层间连线在真
# BaseOperator 上成不成立，只有真 Airflow 说了算（`list >> list` 那个 bug 就是这么漏过去的）。
#
# 镜像默认取本机已有的 2.10.4；**应该指到你的部署实际用来解析 DAG 的那个镜像**，
# 否则 provider 集合不同，绿灯是假的：
#   make dag-parse DAG_PARSE_IMAGE=airflow-hadoop-airflow:latest
DAG_PARSE_IMAGE ?= apache/airflow:2.10.4
DAG_PARSE_DIR := $(ROOT).dagcheck

# 端到端冒烟：一张表，从本体一路走到目标仓里真的有数据（见 scripts/smoke.py 的说明）。
# 需要后端起着（make backend）以及 Airflow / 目标仓可达。换表：SMOKE_ENTITY=item make smoke
smoke:
	@cd "$(BACKEND)" && . .venv/bin/activate && python "$(ROOT)scripts/smoke.py"

dag-parse:
	@cd "$(BACKEND)" && . .venv/bin/activate && \
		python scripts/emit_dag_fixtures.py "$(DAG_PARSE_DIR)"
	@echo "用 $(DAG_PARSE_IMAGE) 解析…"
	@docker run --rm \
		-v "$(DAG_PARSE_DIR)/dags:/opt/airflow/dags:ro" \
		-v "$(ROOT)scripts:/ontometa-scripts:ro" \
		--entrypoint python "$(DAG_PARSE_IMAGE)" \
		/ontometa-scripts/dag_parse_check.py /opt/airflow/dags

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

# ---- 物化编排验证栈（Airflow + SeaTunnel + 可选 Doris），见 docker/orchestration/README.md ----
ORCH := docker compose -f $(ROOT)docker/orchestration/docker-compose.yml

orch-preflight:
	@bash "$(ROOT)docker/orchestration/preflight.sh"

orch-up-airflow: orch-preflight
	$(ORCH) --profile airflow up -d --build

orch-up-sync:
	$(ORCH) --profile sync up -d

orch-up-warehouse:
	$(ORCH) --profile warehouse up -d

orch-up-all: orch-preflight
	$(ORCH) --profile all up -d --build

orch-down:
	$(ORCH) --profile all down

orch-logs:
	$(ORCH) --profile all logs -f --tail=100
