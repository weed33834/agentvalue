# AgentValue Backend

FastAPI + LangGraph + SQLAlchemy backend for the AgentValue employee value evaluation platform.

## Tech Stack

| Technology | Purpose |
|---|---|
| Python 3.11+ | Runtime |
| FastAPI | REST API framework with auto-generated OpenAPI/Swagger docs |
| SQLAlchemy 2.0 | ORM with async support |
| Alembic | Database migration management |
| LangGraph | Agent state machine with `interrupt`-based human-in-the-loop approval |
| ChromaDB | Vector store for semantic memory and knowledge base |
| Redis (optional) | Task queue (arq) and cache |
| Prometheus | Metrics collection (21+ business metrics) |
| Langfuse (optional) | LLM tracing and observability |
| sse-starlette | SSE streaming for AI chat responses |
| pydantic-settings | Environment variable configuration |

## Directory Structure

```
backend/
├── agent/                  # LangGraph agent workflows
│   ├── graph.py            # Evaluation state machine with interrupt approval flow
│   ├── langchain_tools.py  # 9 built-in agent tools
│   ├── session_prompt.py   # AI chat ReAct loop (manual ReAct + SSE + tool execution)
│   ├── session_processor.py# Chat processor (SSE streaming, tool calls, persistence)
│   ├── prompt_loader.py    # Prompt template loader
│   ├── state.py            # State machine definitions
│   └── tools.py            # Memory/knowledge base tools for agents
├── api/                    # FastAPI route handlers
│   ├── auth_routes.py      # Login, register, refresh, logout, me
│   ├── chat.py             # Chat: sessions, messages, regenerate, search, feedback
│   ├── deps.py             # Dependency injection (auth, DB session, permissions)
│   └── routes.py           # All evaluation, employee, manager, hr, admin routes
├── auth/                   # Authentication & authorization
│   ├── jwt_handler.py      # JWT token creation and validation
│   ├── password.py         # Password hashing (bcrypt/argon2)
│   └── rbac.py             # Role-based access control enforcement
├── core/                   # Infrastructure
│   ├── config.py           # Settings via pydantic-settings (reads .env)
│   ├── database.py         # Async SQLAlchemy engine and session factory
│   ├── embeddings.py       # Embedding service abstraction
│   ├── model_router.py     # Model tier routing (cloud/local/mock)
│   ├── multimodal/         # OCR, ASR, image extraction
│   ├── providers/          # LLM provider abstractions (OpenAI, Anthropic, Gemini, etc.)
│   └── tracing.py          # OpenTelemetry / Langfuse tracing
├── data/                   # Demo/seed data files
├── eval/                   # LLM regression evaluation scripts
├── memory/                 # Vector memory abstractions
├── models/                 # SQLAlchemy ORM models
├── prompts/                # Prompt template files (evaluation, chat, etc.)
├── schemas/                # Pydantic request/response schemas
├── scripts/                # Utility scripts
│   ├── seed_kb.py          # Seed knowledge base
│   ├── seed_demo.py        # Seed demo users and data
│   ├── migrate.py          # Alembic migration helper
│   ├── check_prod_readiness.py  # Production readiness check
│   ├── data_retention.py   # Data retention policy enforcement
│   ├── fairness_audit.py   # Bias/fairness audit script
│   ├── gdpr_audit.py       # GDPR compliance audit
│   └── sla_monitor.py      # SLA monitoring
├── services/               # Business logic services
│   ├── chat_service.py     # Chat session and message management
│   ├── evaluation_service.py  # Evaluation orchestration
│   ├── alert_service.py    # Alert notification service
│   └── email_service.py    # Email service
├── tests/                  # 1500+ tests
│   ├── e2e/                # End-to-end API tests (FastAPI TestClient)
│   ├── perf/               # Load tests (Locust)
│   └── enterprise/         # Enterprise feature tests (SSO, LDAP, quotas, etc.)
├── integrations/           # Enterprise integrations
│   ├── feishu/             # Feishu/Lark IM adapter
│   ├── gitlab/             # GitLab code repository adapter
│   └── mcp/                # Model Context Protocol client
├── Dockerfile              # Production container image
├── pyproject.toml          # Python project metadata and tool config
├── requirements.txt        # Python dependencies
├── pytest.ini              # Test configuration
└── alembic.ini             # Alembic migration configuration
```

## Local Development

### Prerequisites

- Python 3.11+
- pip / venv

### Setup

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env — at minimum configure CLOUD_API_KEY or local LLM endpoint
```

### Run

```bash
uvicorn main:app --reload --port 8000
```

### Run Without API Key (Mock Mode)

```bash
cp .env.example .env
# Leave CLOUD_API_KEY empty — Mock Provider auto-activates
uvicorn main:app --reload --port 8000

# Verify with mock evaluation
python -m eval.evaluate --mock
```

## API Overview

### Authentication

| Method | Path | Description |
|---|---|---|
| POST | `/api/v1/auth/login` | Login with email + password |
| POST | `/api/v1/auth/register` | Register new user |
| POST | `/api/v1/auth/refresh` | Refresh access token |
| POST | `/api/v1/auth/logout` | Logout and revoke token |
| GET | `/api/v1/auth/me` | Current user info |
| POST | `/api/v1/auth/seed-demo-users` | Seed demo accounts (dev only) |

### Evaluation

| Method | Path | Description |
|---|---|---|
| POST | `/api/v1/inputs` | Submit raw input (report, task progress, attachments) |
| POST | `/api/v1/evaluations` | Trigger async evaluation; returns `job_id` |
| GET | `/api/v1/evaluations/jobs/{job_id}` | Query evaluation job status |
| GET | `/api/v1/evaluations/{id}` | Get evaluation result (role-filtered) |
| GET | `/api/v1/evaluations/{id}/employee-view` | Employee perspective |
| GET | `/api/v1/evaluations/{id}/manager-view` | Manager perspective |
| GET | `/api/v1/evaluations/{id}/audit-logs` | Evaluation audit trail |
| POST | `/api/v1/evaluations/{id}/approve` | Manager approval |
| POST | `/api/v1/evaluations/{id}/reject` | Reject evaluation |
| POST | `/api/v1/evaluations/{id}/appeal` | Employee appeal |

### Chat (AI Conversation)

| Method | Path | Description |
|---|---|---|
| POST | `/api/v1/chat/sessions` | Create session (optional model_name) |
| GET | `/api/v1/chat/sessions` | List user's sessions |
| PATCH | `/api/v1/chat/sessions/{id}` | Update session (title, model_name) |
| DELETE | `/api/v1/chat/sessions/{id}` | Delete session |
| POST | `/api/v1/chat/sessions/{id}/messages` | Send message (SSE streaming response) |
| POST | `/api/v1/chat/sessions/{id}/regenerate` | Regenerate last assistant response |
| POST | `/api/v1/chat/sessions/{id}/auto-title` | Auto-generate session title via LLM |
| POST | `/api/v1/chat/sessions/{id}/messages/{msg_id}/feedback` | Like/dislike feedback |
| GET | `/api/v1/chat/sessions/search?q=keyword` | Search sessions by title |

### Agent Tools (Chat)

| Tool | Description | Security Constraint |
|---|---|---|
| `bash` | Execute shell commands | 30s timeout, 5000-char truncation |
| `read_file` | Read file contents | 5000-char truncation |
| `write_file` | Write to files | Auto-creates parent directories |
| `list_directory` | List directory contents | — |
| `web_fetch` | Fetch web pages | HTML-to-text, truncated |
| `calculator` | Math expressions | — |
| `get_current_datetime` | Current date/time | — |
| `get_employee_history` | Employee past evaluations | Business tool |
| `query_company_kb` | Company knowledge base | Business tool |

### Admin API

| Method | Path | Description |
|---|---|---|
| GET | `/api/v1/admin/model-status` | Model status and recommended tier |
| POST | `/api/v1/admin/model-switch` | Switch model tier |
| GET | `/api/v1/admin/audit-logs` | Audit log query (paginated) |
| + 22 enterprise module APIs | `/api/v1/admin/*` | SSO, quotas, billing, LLM judge, etc. |

Full OpenAPI documentation at `http://localhost:8000/docs` when running.

## Database Migrations

Uses Alembic for schema migration:

```bash
# View current version
alembic current

# View migration history
alembic history

# Upgrade to latest
alembic upgrade head

# Rollback one version
alembic downgrade -1

# Auto-generate migration from model changes
alembic revision --autogenerate -m "Description of change"

# Or use the helper script
python scripts/migrate.py upgrade
```

## Model Tiers

| Tier | Use Case | Example Models |
|---|---|---|
| `auto` | Auto-detect from hardware (default) | — |
| `L0` | Cloud flagship | GPT-4o, DeepSeek-V3, Qwen-Max |
| `L1` | Edge/small local | Qwen2.5-0.5B |
| `L2` | Standard local | Qwen2.5-7B |
| `L3` | Local flagship | Qwen2.5-14B |

## Role Permissions

| Role | Permissions |
|---|---|
| `employee` | View own employee view, submit inputs, feedback and appeals |
| `manager` | Approve evaluations, view manager view, team analysis |
| `hr` | Review high-risk evaluations, view audit logs |
| `admin` | Switch model tiers, view all audit logs, full admin console |

## Testing

```bash
# Unit tests
python -m pytest tests -q

# E2E tests
python -m pytest -m e2e -q

# Mock evaluation (no API key needed)
python -m eval.evaluate --mock

# Load tests
locust -f tests/perf/locustfile.py --headless -u 100 -r 10

# Enterprise tests
python -m pytest tests/enterprise/ -q

# Production readiness check
python scripts/check_prod_readiness.py
```

## Environment Variables

All configuration via `backend/.env`. See [.env.example](.env.example) for the complete reference with detailed descriptions for each variable.

Key variables you'll likely need to customize:

| Variable | Why Change It |
|---|---|
| `JWT_SECRET_KEY` | **Production requirement** — generate a strong random secret |
| `AGENTVALUE_ENV=production` | **Production requirement** — enables safety guards |
| `CLOUD_API_KEY` | To use cloud LLM models |
| `CORS_ORIGINS` | **Production requirement** — set to your frontend domain |
| `DATABASE_URL` | Switch to PostgreSQL for production |
| `FIELD_ENCRYPTION_KEY` | **Production requirement** — encrypts sensitive DB fields |
