.PHONY: help install dev test lint format build docker docker-prod clean

PYTHON ?= python3
NODE ?= npm

help: ## 显示所有可用命令
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ────────────────── 安装 ──────────────────

install: install-backend install-frontend ## 安装所有依赖

install-backend: ## 安装后端依赖
	cd backend && pip install -r requirements.txt

install-frontend: ## 安装前端依赖
	cd frontend && $(NODE) install

# ────────────────── 开发 ──────────────────

dev: dev-backend dev-frontend ## 启动前后端开发服务器(需两个终端)

dev-backend: ## 启动后端开发服务器
	cd backend && LOG_LEVEL=DEBUG $(PYTHON) -m uvicorn main:app --reload --port 8000

dev-frontend: ## 启动前端开发服务器
	cd frontend && $(NODE) run dev

# ────────────────── 测试 ──────────────────

test: test-backend test-frontend ## 运行所有测试

test-backend: ## 运行后端测试
	cd backend && DEMO_MODE=true $(PYTHON) -m pytest -x -q

test-frontend: ## 运行前端测试
	cd frontend && $(NODE) run test

# ────────────────── 代码检查 ──────────────────

lint: lint-backend lint-frontend ## 运行所有 lint

lint-backend: ## 后端 ruff + black 检查
	cd backend && ruff check . && black --check .

lint-frontend: ## 前端 eslint + prettier 检查
	cd frontend && $(NODE) run lint && $(NODE) run format:check

format: ## 格式化所有代码
	cd backend && ruff format . && black .
	cd frontend && $(NODE) run format

# ────────────────── 构建 ──────────────────

build: build-frontend ## 构建前端生产包

build-frontend: ## 构建前端
	cd frontend && $(NODE) run build

# ────────────────── Docker ──────────────────

docker: ## 用 docker-compose 启动全部服务
	docker compose up -d --build

docker-prod: ## 用生产配置启动
	docker compose -f docker-compose.prod.yml up -d --build

docker-down: ## 停止 docker-compose 服务
	docker compose down

# ────────────────── 数据库 ──────────────────

migrate: ## 运行数据库迁移
	cd backend && $(PYTHON) -m alembic upgrade head

migrate-create: ## 创建新迁移 (用法: make migrate-create MSG="描述")
	cd backend && $(PYTHON) -m alembic revision --autogenerate -m "$(MSG)"

seed: ## 填充演示数据
	cd backend && $(PYTHON) -m scripts.seed_demo

# ────────────────── 清理 ──────────────────

clean: ## 清理构建产物和缓存
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name node_modules -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name dist -exec rm -rf {} + 2>/dev/null || true
	rm -f backend/*.db backend/*.db-shm backend/*.db-wal
	rm -rf backend/chroma_db
