# AgentValue Frontend

Vue 3 + Vite + Element Plus frontend for the AgentValue employee value evaluation and AI conversational platform.

## Tech Stack

| Technology | Purpose |
|---|---|
| Vue 3 (Composition API, JavaScript) | Component framework |
| Vite 8 | Build tool and dev server |
| Element Plus | UI component library |
| Pinia | State management |
| Vue Router 5 | Client-side routing with role-based guards |
| ECharts | Data visualization (radar charts, trend lines, 9-box grids) |
| Vue Flow | Workflow/agent graph visualization |
| KaTeX | Math formula rendering (`$...$` / `$$...$$`) |
| Mermaid | Diagram rendering (flowcharts, sequence diagrams) |
| SSE (custom) | Streaming response client for AI chat |
| Vitest 4 | Unit testing |
| Playwright | E2E testing and runtime smoke tests |

## Project Structure

```
frontend/
├── public/                          # Static assets
│   ├── favicon.svg                  # Brand logo (blue-violet gradient + geometric "A")
│   ├── pwa-192x192.png              # PWA icon (192px)
│   ├── pwa-512x512.png              # PWA icon (512px)
│   └── robots.txt
├── src/
│   ├── components/                  # Reusable Vue components
│   │   ├── BrandLogo.vue            # Unified brand logo (mark + wordmark)
│   │   ├── Watermark.vue            # Security watermark for management views
│   │   └── chat/                    # AI Chat components
│   │       ├── ChatInput.vue        # Multi-line input + file upload
│   │       ├── ChatView.vue         # Chat main view (sessions, messages, model switch)
│   │       ├── MessageBubble.vue    # Message: Markdown, code, tools, feedback
│   │       ├── MessageList.vue      # Message list container with auto-scroll
│   │       └── ToolCallCard.vue     # Tool call I/O display (collapsible)
│   ├── layouts/
│   │   └── MainLayout.vue           # Main layout (sidebar, header, router-view)
│   ├── views/
│   │   ├── LoginView.vue            # Desktop login (JWT auth + Demo mode)
│   │   ├── employee/                # Employee portal (dashboard, input, history, etc.)
│   │   ├── manager/                 # Manager portal (team, ROI, risk, approval)
│   │   ├── hr/                      # HR portal (review queue, audit detail)
│   │   ├── admin/                   # Admin console (40+ management pages)
│   │   └── mobile/                  # Mobile-optimized pages (/m/* routes, 4 roles)
│   ├── stores/                      # Pinia state stores (auth, chat, notification, theme)
│   ├── router/index.js              # Vue Router with role-based navigation guards
│   ├── utils/                       # Utilities (markdown renderer, SSE client, etc.)
│   └── styles/global.css            # Global CSS variables and theme
└── index.html                       # Entry point
```

## Route Map

| Route | Role(s) | Page |
|---|---|---|
| `/login` | All | Desktop login (JWT auth + demo mode) |
| `/employee` | employee | Growth dashboard, radar chart |
| `/employee/input` | employee | Daily report, task progress, file upload |
| `/employee/history` | employee | Past evaluation history |
| `/employee/feedback` | employee | Feedback and grievance tracking |
| `/employee/growth-path` | employee | Growth path recommendation |
| `/employee/assistant` | employee | AI assistant |
| `/manager` | manager, admin | Team value ranking, risk distribution |
| `/manager/team` | manager, admin | Team analysis, dimension comparison |
| `/manager/roi` | manager, admin | ROI 9-box grid, weekly trends |
| `/manager/attrition-risk` | manager, admin | Attrition risk prediction |
| `/manager/reviews-360` | manager, admin | 360° peer reviews |
| `/manager/calibration` | manager, admin | Calibration meeting |
| `/manager/assistant` | manager, admin | AI assistant |
| `/hr` | hr, admin | HR review queue |
| `/hr/assistant` | hr, admin | AI assistant |
| `/admin` | admin | Model management dashboard |
| `/admin/llm-config` | admin | LLM configuration center |
| `/admin/chat` | All | AI Chat interface (full-featured) |
| `/admin/audit-logs` | admin | Audit log viewer |
| `/admin/metrics` | admin | System metrics |
| + 35+ admin routes | admin | Various management pages (feature flags, billing, etc.) |
| `/m/*` | All | Mobile-optimized versions of all routes |

## Development

```bash
# Install dependencies
cd frontend && npm install

# Start dev server (http://localhost:5173)
# Vite auto-proxies /api/* to http://localhost:8000 (backend)
npm run dev

# Run unit tests
npx vitest run

# Run lint
npm run lint

# Build for production
npm run build         # Output: dist/
```

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `VITE_API_BASE_URL` | `/api/v1` | Backend API base path (proxied by Vite in dev mode) |

## AI Chat Components (v1.5.0+)

Full-featured conversational AI with SSE streaming, tool calls, session management:

| Component | File | Purpose |
|---|---|---|
| `ChatView.vue` | `views/admin/ChatView.vue` | Main view — session list + message stream + model switch + export |
| `MessageList.vue` | `components/chat/MessageList.vue` | Message container with auto-scroll and event dispatch |
| `MessageBubble.vue` | `components/chat/MessageBubble.vue` | Markdown rendering + code copy + inline edit + like/dislike + reasoning fold |
| `ToolCallCard.vue` | `components/chat/ToolCallCard.vue` | Tool call: collapsible I/O, JSON beautify, status icons |
| `ChatInput.vue` | `components/chat/ChatInput.vue` | Multi-line text + file upload + attach preview + send/stop |
| `chat.js` | `stores/chat.js` | SSE event dispatch, streaming render, tool calls, session CRUD |
| `markdown.js` | `utils/markdown.js` | KaTeX math + Mermaid diagrams + code highlighting |
| `sse.js` | `utils/sse.js` | SSE client: JWT auth, AbortController, heartbeat filter |

## Mobile Adaptation

The `/m/*` route namespace provides a fully separate mobile experience for all four roles. Mobile pages are auto-detected via `navigator.userAgent` and redirected from desktop routes (unless `?desktop=1` is specified). See `src/views/mobile/` for implementation.

## Testing

```bash
# Unit tests
npx vitest run

# E2E smoke tests (requires dev server running)
npx playwright test

# Lint
npm run lint
```
