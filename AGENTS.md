# AGENTS.md — Wishkr Backend

> This file governs all AI-assisted engineering for `wishkr-backend`. Read and adhere to every section before inspecting, modifying, or creating code.

---

## 1. Codebase Navigation: Graphify Over Grep/Find (Mandatory)

This repository maintains a Graphify knowledge graph in `graphify-out/`.

### 🚨 Strict Navigation Rules
- **NEVER use raw `grep`, `ripgrep`, or `find` as your primary architectural exploration tool.**
- **ALWAYS consult the Graphify knowledge graph first** to navigate architecture, dependencies, community clusters, and god nodes.
- Inspect `graphify-out/GRAPH_REPORT.md` before answering architecture questions or planning major refactors.
- **After modifying code files in any session, ALWAYS run `graphify update .`** to keep the graph in sync (AST-only, zero LLM cost).

### Graphify CLI Cheat-Sheet & Parameters
| Command | Options & Parameters | When to Use |
| :--- | :--- | :--- |
| `graphify query "<question>"` | `--dfs` (deep call chain)<br>`--budget <N>` (token cap, default 2000)<br>`--context <C>` (filter by edge type)<br>`--graph <path>` | Semantic & structural questions across modules, handlers, and models. |
| `graphify path "<NodeA>" "<NodeB>"` | `--graph <path>` | Traces the exact shortest dependency/call path between two components (e.g. webhook handler $\rightarrow$ model). |
| `graphify explain "<Symbol/File>"` | `--graph <path>` | Plain-language breakdown of a component, its incoming/outgoing callers, and its cluster neighbors. |
| `graphify update .` | `--force` (overwrite if fewer nodes)<br>`--no-viz` (skip HTML viz) | Re-extracts AST and updates `graphify-out/graph.json` without API costs. |
| `graphify tree` | `--root <path>`<br>`--top-k-edges <N>` | Generates a hierarchical D3 tree of the project architecture. |

---

## 2. Ponytail — Lazy Senior Dev Mode (Efficiency Over Bloat)

> *"The best code is the code you never wrote. Lazy means efficient, not careless."*

Before writing or suggesting any code, stop at the first rung that holds:

```
          ┌────────────────────────────────────────────────────────┐
          │  1. Does this need to exist at all? (YAGNI)            │
          ├────────────────────────────────────────────────────────┤
          │  2. Already in this codebase? (Reuse existing utils)   │
          ├────────────────────────────────────────────────────────┤
          │  3. Does the Python standard library do it?            │
          ├────────────────────────────────────────────────────────┤
          │  4. Native platform / DB constraint covers it?         │
          ├────────────────────────────────────────────────────────┤
          │  5. Does an already-installed dependency solve it?     │
          ├────────────────────────────────────────────────────────┤
          │  6. Can it be one clean line?                          │
          ├────────────────────────────────────────────────────────┤
          │  7. Only then: write the minimum code that works.      │
          └────────────────────────────────────────────────────────┘
```

### Core Ponytail Rules:
- **No unrequested abstractions:** No interfaces with only one implementation, no factories for one object, no dynamic configs for values that never change.
- **Root-Cause Fixes:** Never patch a symptom at one caller. Inspect all callers of the shared function and fix the root cause once.
- **Boring Over Clever:** Write straightforward, idiomatic code that won't require a 3 AM debugging session.
- **Shortest Working Diff:** Deletion over addition. Smallest necessary diff wins once the problem is fully understood.

---

## 3. Technology Stack & Architectural Patterns

- **Framework:** FastAPI (Python 3.11+)
- **Database & ORM:** PostgreSQL + SQLAlchemy 2.0 Async (`asyncpg`).
- **Validation & Schemas:** Pydantic v2 (`BaseModel`, `field_validator`, `ConfigDict`).
- **AI / LLM Agent:** Google ADK (`LlmAgent`, `Runner`, `DatabaseSessionService`, Gemini 2.5/Flash).
- **Background Tasks:** APScheduler + Celery workers.
- **External Channels:** Meta WhatsApp Cloud API (Webhooks & Templates), Razorpay, Hubble Fulfillment.

---

## 4. Backend Engineering Laws

### ⚡ Async & Database Rules
- **All I/O operations must be `async`:** DB queries, HTTP calls, redis, and file I/O must use `async def` and `await`.
- **SQLAlchemy 2.0 syntax only:** Use `select(...)`, `update(...)`, `delete(...)` with `await db.execute(...)` or `await db.scalars(...)`. Never use legacy Query API (`session.query()`).
- **Dependency Injection:** Inject DB sessions via `Annotated[AsyncSession, Depends(get_db)]`.
- **Database Migrations:**
  - Never manually alter database tables in production or dev.
  - Generate migrations via `alembic revision --autogenerate -m "description"`.
  - Inspect generated migration scripts before running `alembic upgrade head`.

### 🛡️ Pydantic v2 Conventions
- Use `model_config = ConfigDict(...)` instead of inner `class Config:`.
- Use `@field_validator('field')` with `@classmethod` instead of v1 `@validator`.
- Use `model_dump()` and `model_validate()` instead of deprecated `dict()` and `parse_obj()`.

### 🤖 LLM & ADK Agent Rules
- **Prompt Stability:** Maintain byte-stable static prompts in `app/bot/prompt.py` to maximize Gemini implicit prompt caching. Dynamic variables (date, user context) must go in turn context, NOT the static system prompt.
- **Deterministic Action Router First:** Pre-filter deterministic intents (menus, simple links, cancellations) in `app/bot/action_router.py` to save LLM tokens and prevent hallucination loops.
- **Feature Flags:** Respect `settings.mvp_reminder_only` and gating flags (`ENABLE_SHARED_WISHLIST`, `PREFERENCE_OUTBOUND_ENABLED`). Never bypass safety controls.

### 📱 Mobile App (API) First
- When building new features or endpoints for the mobile app (`wishkr_app`), design clean, stateless REST endpoints under `/api/v1/`.
- Return consistent JSON error envelopes: `{"error": "error_code", "message": "Human readable hint"}`.

---

## 5. Testing & Verification

- Test suite runner: `pytest`
- Format & lint: `ruff check . --fix` and `ruff format .`
- Type checking: `mypy app`
- Run specific tests:
  ```bash
  pytest tests/test_feature_flags.py -v
  pytest tests/test_occasions.py -v
  ```
