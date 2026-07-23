[English](README.md) | [中文](README.zh.md) | [日本語](README.ja.md)

<p align="center">
  <img src="docs/assets/logo.jpg" width="180" alt="AgentValue-AI Logo" />
</p>

<h1 align="center">AgentValue-AI</h1>

<p align="center">
  An AI agent platform that combines conversational chat, computer-use tooling,<br/>
  and structured employee value evaluation. Comparable in scope to ChatGPT / Claude.ai / opencode / Dify / Coze.
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-CNCL%20v1.0-red.svg" alt="License" /></a>
  <a href="https://github.com/weed33834/agentvalue/actions/workflows/ci.yml"><img src="https://github.com/weed33834/agentvalue/actions/workflows/ci.yml/badge.svg" alt="CI" /></a>
  <a href="https://github.com/weed33834/agentvalue/actions/workflows/security.yml"><img src="https://github.com/weed33834/agentvalue/actions/workflows/security.yml/badge.svg" alt="Security" /></a>
  <img src="https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white" alt="Python" />
  <img src="https://img.shields.io/badge/Node-20+-339933?logo=node.js&logoColor=white" alt="Node" />
  <img src="https://img.shields.io/badge/FastAPI-0.139+-009688?logo=fastapi&logoColor=white" alt="FastAPI" />
  <img src="https://img.shields.io/badge/Vue-3-4FC08D?logo=vue.js&logoColor=white" alt="Vue 3" />
  <img src="https://img.shields.io/badge/LangGraph-agent-FF6B6B" alt="LangGraph" />
  <img src="https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white" alt="Docker" />
  <a href="CHANGELOG.md"><img src="https://img.shields.io/badge/version-2.2.0-blue.svg" alt="Version" /></a>
  <a href="CONTRIBUTING.md"><img src="https://img.shields.io/badge/PRs-welcome-brightgreen.svg" alt="PRs Welcome" /></a>
</p>

---

## Overview

AgentValue-AI consolidates three capability areas into a single platform:

**Conversation** — A full-featured AI chat interface with streaming output, tool-call visualization, collapsible reasoning traces, math rendering, and Markdown export. It covers the interactions commonly used in ChatGPT or Claude.ai.

**Tool use** — The agent executes bash commands, reads and writes files, lists directories, fetches web pages, searches code, and runs a Python sandbox. Built-in tools cover day-to-day operations, and the MCP protocol allows integration with 400+ external tools. High-risk tools can be toggled per environment in production.

**Employee value evaluation** — The platform ingests multi-dimensional work data (daily reports, task progress, code contributions, meeting notes, screenshots, voice), analyzes it through a LangGraph agent, and produces three separate views in a single inference pass:

| View | Audience | Content |
|---|---|---|
| Employee | The individual | Constructive growth feedback |
| Manager | Direct manager / HR | Talent diagnostics and reallocation suggestions |
| Audit | Compliance / audit | Each conclusion carries original-evidence citations and is traceable |

The three views are intentionally separated. The same finding is phrased as "growth room" to the employee and as "ROI decline" to the manager — wording and stance differ by audience. Every evaluation must pass manual approval before it takes effect; this is a hard constraint, not a recommendation.

---

## Features

### AI Chat

| Feature | Description |
|---|---|
| Streaming chat | SSE token-by-token output, supports interruption |
| Tool-call display | Collapsible input/output, JSON prettified, status icons |
| Reasoning trace | DeepSeek `reasoning_content` shown in a collapsible block |
| Message copy | Code-block copy and whole-message copy |
| Regenerate | Drop the last reply and re-run |
| Edit message | Inline editing of user messages |
| Token usage | Per-message token breakdown and response latency |
| Session management | Rename, auto-title, search, Markdown export |
| Math formulas | KaTeX inline `$...$` and block `$$...$$` |
| Chart rendering | Mermaid flowcharts and sequence diagrams, lazy-loaded |
| Feedback | Like / dislike, persisted |
| File upload | Multi-file attachments, 10 MB cap |
| Model switching | Dropdown across 8 models |

### Agent Tools

| Tool | Description | Safety constraint |
|---|---|---|
| `bash` | Execute shell commands | 30 s timeout + 5 000-char truncation |
| `read_file` | Read file contents | 5 000-char truncation |
| `write_file` | Write files | Auto-creates parent directories |
| `list_directory` | List directory contents | — |
| `web_fetch` | Fetch web pages | HTML → plain text + truncation |
| `calculator` | Math evaluation | — |
| `get_current_datetime` | Return current date and time | — |
| `get_employee_history` | Query an employee's evaluation history | Business tool |
| `query_company_kb` | Query the company knowledge base | Business tool |

Tools are managed centrally through `ToolRegistry` and can be toggled via the `enabled_tools` configuration.

### Operations Console

| Page | Route | Comparable to |
|---|---|---|
| Model providers | `/admin/providers` | Dify model-providers |
| Prompt playground | `/admin/playground` | Langfuse Playground |
| Knowledge base | `/admin/knowledge-base` | Dify Dataset |
| Trace | `/admin/trace` | Langfuse Trace |
| Token trends | `/admin/metrics` | Langfuse Usage |
| Feature flags | `/admin/feature-flags` | LaunchDarkly |
| Multi-agent collaboration | `/admin/multi-agent` | LangGraph Supervisor |
| Workflow orchestration | `/admin/workflows` | Dify Workflow |
| Custom tools | `/admin/tools` | Dify Custom Tool |
| Model fallback | `/admin/model-fallback` | Alibaba Bailian AI Gateway |
| Session analytics | `/admin/analytics-v2` | Langfuse Dashboard |
| API health | `/admin/api-health` | Langfuse latency monitoring |
| Datasets | `/admin/datasets` | Langfuse datasets |
| LLM judge | `/admin/llm-judge` | Langfuse LLM-as-a-Judge |
| RAG evaluation | `/admin/rag-eval` | RagFlow retrieval testing |
| Human annotation | `/admin/annotations` | Langfuse HITL |
| SSO | `/admin/sso` | Dify SSO |
| Template marketplace | `/admin/agent-templates` | Coze plugin market |
| NL2SQL | `/admin/nl2sql` | RagFlow NL2SQL |
| Document parsing | `/admin/doc-parsing` | RagFlow DeepDoc |
| Quota management | `/admin/quota` | Alibaba Bailian Token Plan |
| Budget alerts | `/admin/budgets` | Langfuse budgets |
| Billing | `/admin/billing` | Alibaba Bailian unified metering |
| Agent versions | `/admin/agent-versions` | Langfuse versions |
| Multi-channel publish | `/admin/publish` | Coze omnichannel distribution |
| Sensitive words | `/admin/sensitive-words` | Tencent Hunyuan content safety |
| Alerts | `/admin/alerts` | Grafana Alerting |
| Tool config | `/admin/tool-config` | Dify tool management |
| Hybrid retrieval | `/admin/search` | Dify retrieval |

---

## System Architecture

```
┌──────────────────────────────────────────────────────────┐
│        Frontend Layer (Vue 3 + Element Plus)             │
│  Employee │ Manager │ HR │ Admin Console │ AI Chat UI     │
├──────────────────────────────────────────────────────────┤
│        API Gateway Layer (FastAPI)                       │
│  RBAC │ Rate limiting │ Audit logs │ Guards │ SSE stream  │
├──────────────────────────────────────────────────────────┤
│     Agent Orchestration Layer (LangGraph + ReAct loop)   │
│  State machine │ Tool calls │ Memory retrieval │ HITL     │
├──────────────────────────────────────────────────────────┤
│        Agent Tool Layer (9 built-in tools)               │
│  bash │ read_file │ write_file │ list_directory │ web_fetch│
│  calculator │ datetime │ employee_history │ company_kb    │
├──────────────────────────────────────────────────────────┤
│        Model Abstraction Layer (ModelRouter)             │
│  Hardware probe │ Cloud API │ Local LM Studio │ Fallback  │
├──────────────────────────────────────────────────────────┤
│        Data and Memory Layer                             │
│  SQLite/PostgreSQL │ ChromaDB │ Redis (queue)            │
└──────────────────────────────────────────────────────────┘
```

---

## Tech Stack

| Layer | Technology |
|---|---|
| Frontend | Vue 3 + JavaScript + Vite + Element Plus + ECharts + Vue Flow + KaTeX + Mermaid |
| Backend | Python 3.11+ + FastAPI + SQLAlchemy |
| Agent | LangGraph (supervisor multi-agent + ReAct loop + SSE streaming) |
| LLM Provider | OpenAI / Anthropic Claude / Google Gemini / Ollama (encrypted credentials + load balancing) |
| Rerank | Cohere / Jina / BGE (local) / Dummy fallback |
| Streaming | sse-starlette + @microsoft/fetch-event-source |
| Vector memory | ChromaDB |
| Database | SQLite (default) / PostgreSQL (production) |
| Cache | Redis (job queue; in-memory fallback when unconfigured) |
| Observability | Prometheus + Langfuse + Grafana |
| Workflow engine | In-house DAG executor (Kahn topological sort + 7 node types + code sandbox) |
| Feature flag | In-house 5-level rules (sha256 stable hash + 60 s LRU cache) |
| Tests | pytest + locust (1 517 + 122 enterprise = 1 639 passing) |
| Deployment | Docker Compose |
| Safety guardrails | InputGuard + OutputGuard (PII redaction / jailbreak defense / bias detection / hallucination flagging) |

---

## Quick Start

### Docker Compose (one command)

```bash
git clone https://github.com/weed33834/agentvalue.git
cd agentvalue
cp backend/.env.example backend/.env
docker compose up -d --build
```

After startup, the following services are available:

| Service | URL |
|---|---|
| Frontend | <http://localhost> |
| Backend API | <http://localhost:8000> |
| Health check | <http://localhost:8000/health> |
| Swagger UI | <http://localhost:8000/docs> |

### Local development

**Backend:**

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                  # Fill in model API keys
uvicorn main:app --reload --port 8000
```

**Frontend:**

```bash
cd frontend
npm install
npm run dev                           # http://localhost:5173
```

### Running without an API key

With no model API key configured, the system automatically uses a Mock Provider and runs the evaluation flow end-to-end:

```bash
cd backend
cp .env.example .env
AUTH_DEMO_MODE=true uvicorn main:app --reload
python -m eval.evaluate --mock        # Run evaluation without external APIs
```

> Demo mode is for local development only. `AUTH_DEMO_MODE=true` allows identity spoofing via HTTP headers and must be disabled before deploying to any non-local environment.

---

## Configuration

All configuration is injected through `backend/.env`. See [backend/.env.example](backend/.env.example) for per-item annotations.

### Key configuration

| Variable | Purpose | When it must be changed |
|---|---|---|
| `JWT_SECRET_KEY` | JWT signing key | Production deployment |
| `AGENTVALUE_ENV` | Set to `production` to enable production guards | Production deployment |
| `CLOUD_API_KEY` | Cloud LLM (OpenAI-compatible) | When real models are required |
| `EMBEDDING_API_KEY` | Real embedding service | When semantic retrieval is required |
| `CORS_ORIGINS` | Allowed frontend origins | Required in production with the real domain |
| `FIELD_ENCRYPTION_KEY` | AES-GCM encryption for sensitive fields | Required in production |

### Model tiers

`MODEL_TIER` controls whether the evaluation LLM runs in the cloud or locally:

| Tier | Scenario | Example models |
|---|---|---|
| `auto` | Auto-recommended from hardware (default) | — |
| `L0` | Cloud large model | GPT-4o / DeepSeek-V3 / Qwen-Max |
| `L1` | Edge small model | Qwen2.5-0.5B |
| `L2` | Standard local model | Qwen2.5-7B |
| `L3` | Local flagship model | Qwen2.5-14B |

When neither `CLOUD_API_KEY` nor `LOCAL_BASE_URL` is configured, the Mock Provider is used and no external model is required.

---

## Usage Guide

### 1. Seed initial data

```bash
python -m scripts.seed_kb             # Company KB (scoring criteria, values, training material)
python -m scripts.seed_demo            # Demo data (users and one sample evaluation)
```

### 2. Log in

Four roles: `employee` / `manager` / `hr` / `admin`.

In demo mode, the login page provides a "fill demo account" button. In normal mode, use `/api/v1/auth/register` and `/api/v1/auth/login` to obtain a JWT.

### 3. Trigger an evaluation

```bash
curl -X POST http://localhost:8000/api/v1/evaluations \
  -H "Authorization: Bearer <admin-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "employee_id": "E1001",
    "period": "2026-W25",
    "raw_inputs": [
      {"type": "daily_report", "content": "Refactored the order-center API today..."},
      {"type": "task_progress", "content": "JIRA-2051 moved to integration stage..."}
    ]
  }'
```

The evaluation enters the LangGraph state machine:

```
input_clean → multimodal_extract → llm_evaluate → parse_output → persist
                       ↑                                ↓
                   retrieve_context            human-in-the-loop interrupt
```

### 4. Approval and the three views

Evaluation status flow:

```
ai_drafted → manager_review → hr_audit (high risk only) → approved/rejected
                                  ↓ rejected
                            employee_appeal → manager_review
```

View the three views:

```bash
curl http://localhost:8000/api/v1/evaluations/{id} \
  -H "Authorization: Bearer <token>"
# employee_view / manager_view / audit_view in the response are the three views.
# Field-level visibility is enforced by RBAC: an employee token cannot see manager_view / audit_view.
```

### 5. AI chat

The admin console exposes a full chat interface at `/admin/chat`. After creating a session, send a message:

```bash
# Create a session
curl -X POST http://localhost:8000/api/v1/chat/sessions \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"title": "Test session", "model_name": "DeepSeek-V4-Flash"}'

# Send a message (SSE streaming response)
curl -X POST http://localhost:8000/api/v1/chat/sessions/{id}/messages \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"content": "List the files in the current directory"}'
```

The agent automatically invokes the `list_directory` tool and returns a formatted result.

### 6. Observability

- Prometheus metrics: <http://localhost:8000/metrics> (21 business metrics)
- Grafana dashboard: <http://localhost:3000> after the production compose stack starts
- Langfuse tracing: auto-reported once `LANGFUSE_*` is configured
- Audit logs: all write operations are written to the audit table, paginated and queryable from the admin console

---

## Testing

```bash
cd backend && python -m pytest tests -q          # Unit tests
cd backend && python -m pytest -m e2e -q         # E2E tests
cd backend && python -m eval.evaluate --mock      # Mock evaluation
cd frontend && npm run lint                       # Frontend lint
cd frontend && npm run build                      # Frontend build
```

The backend has 1 517 passing tests; the frontend build and lint produce no errors.

---

## Production Deployment

```bash
cp backend/.env.example backend/.env
# Edit .env and set all production credentials
cd backend && python scripts/check_prod_readiness.py   # Readiness check
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

The production stack layers PostgreSQL, MinIO, and Prometheus + Grafana on top of the base compose file.

Detailed deployment guides: [Enterprise deployment](docs/deployment-guide.md) | [Pilot runbook](docs/pilot-runbook.md) | [Scale-out runbook](docs/scale-deployment-runbook.md)

---

## Project Structure

```
.
├── backend/
│   ├── agent/            # LangGraph agent + ReAct loop + tool calls
│   ├── api/              # FastAPI routes (chat / auth / admin/*)
│   ├── auth/             # JWT + RBAC
│   ├── core/             # Config / model router / guards / workflow engine / feature flag
│   ├── models/           # SQLAlchemy data models
│   ├── services/         # Business services
│   ├── tests/            # 1 517 + 122 enterprise = 1 639 passing
│   └── ...
├── frontend/
│   ├── src/components/chat/   # Chat components
│   ├── src/stores/chat.js     # Chat state management
│   ├── src/utils/markdown.js  # KaTeX + Mermaid rendering
│   └── src/views/admin/       # Admin console pages
├── docs/                 # Project documentation
├── monitoring/           # Prometheus configuration
├── grafana/              # Grafana dashboard
├── .github/              # CI / issue templates / PR template
├── docker-compose.yml    # Development stack
├── docker-compose.prod.yml
└── CHANGELOG.md
```

---

## Documentation Index

| Document | Description |
|---|---|
| [CHANGELOG.md](CHANGELOG.md) | Version history |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Contribution guide |
| [SECURITY.md](SECURITY.md) | Security vulnerability reporting process |
| [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) | Code of conduct |
| [backend/README.md](backend/README.md) | Backend development notes |
| [frontend/README.md](frontend/README.md) | Frontend development notes |
| [docs/architecture-notes.md](docs/architecture-notes.md) | Architecture implementation notes |
| [docs/deployment-guide.md](docs/deployment-guide.md) | Enterprise deployment guide |
| [docs/dev-guidelines.md](docs/dev-guidelines.md) | Development guidelines |
| [docs/DEVELOPMENT-PLAN.md](docs/DEVELOPMENT-PLAN.md) | Development plan |

---

## Roadmap

**Released versions:**

- v1.2 — Model provider management + Prompt playground + multi-provider integration
- v1.3 — arq job queue + Postgres persistence + test backfill + CI hardening + Feishu/GitLab integration skeleton
- v1.4 — Knowledge base UI / tracing / token trends / rerank / custom tools / feature flags / multi-agent / workflow orchestration
- v1.5 — AI chat system (10 features) + agent tool layer (5 tools)
- v2.1 — Deep parity with major platforms on the management matrix: model fallback / session analytics / API health / datasets / LLM judge / RAG eval / human annotation / SSO / template marketplace / NL2SQL / document parsing + 19 security hardening items

**Planned directions:**

- Chat attachment parsing (image / PDF / audio)
- Streaming interruption recovery and conversation branching
- Conversation share links
- Multimodal capability completion (cloud OCR / Whisper ASR)
- Team ROI nine-grid and growth-path dashboard enhancements
- IM integration landing (Feishu)
- Code repository integration landing (GitLab)

If there is a direction you would like to push forward, propose it via [GitHub Issues](https://github.com/weed33834/agentvalue/issues).

---

## FAQ

**Can it run without an API key?**

Yes. The system defaults to the Mock Provider and runs the evaluation flow end-to-end, but the LLM output is simulated. Real use requires configuring `CLOUD_API_KEY` or `LOCAL_BASE_URL`.

**Can evaluation results be used directly for HR decisions?**

No. "AI does not make HR decisions" is a core hard constraint: every evaluation must be approved by a manager, and high-risk items additionally require HR review. The agent only generates and structures the output; it does not make decisions on behalf of humans.

**Is the agent's bash tool safe?**

It enforces a 30-second timeout and a 5 000-character output truncation. All tools are managed centrally through `ToolRegistry` and can be toggled via `enabled_tools`. In production, only safe tools such as `calculator,get_current_datetime` can be enabled.

**Which models does the AI chat support?**

The default is `DeepSeek-V4-Flash` (OpenAI-compatible gateway). The frontend dropdown supports DeepSeek V4 Flash/Pro, GLM 4.7/5.1, Qwen 3 Coder, Kimi K2.6, and MiniMax M3. Any provider that supports an OpenAI-compatible API with function calling can be integrated.

**How is multi-tenant isolation implemented?**

At the data layer, every table carries a `tenant_id` column and RBAC filters at the data level. The vector store separates collections by tenant. The job queue prefixes keys with the tenant.

---

## Contributing

Issues and pull requests are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) before starting. CI runs lint / tests / build on every PR; all checks must pass before merge.

---

## Security

If you discover a security vulnerability, follow the process in [SECURITY.md](SECURITY.md) and report it privately. Do not open a public issue.

---

## Mirror

| Platform | URL | Notes |
|---|---|---|
| GitCode (primary) | <https://gitcode.com/badhope/agentvalue> | Issue / PR submission |
| GitHub (mirror) | <https://github.com/weed33834/agentvalue> | International mirror |

---

## License

This project is open-sourced under the [Custom Non-Commercial License (CNCL) v1.0](LICENSE). © 2026 AgentValue-AI Contributors.
