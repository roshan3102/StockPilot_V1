# StockPilot

AI-assisted procurement for a small liquor store: ask inventory questions, turn a supplier rebate into a governed promotion, draft a purchase order with evidence, then approve and pay. The LLM interprets language and orchestrates tools; deterministic services own quantities, prices, totals, approval, and payment verification.

## Prerequisites

- **Python 3.13+** and [`uv`](https://docs.astral.sh/uv/)
- **Node.js 20+** (for the Next.js frontend)
- An **OpenAI API key** (required for chat, restock runs, and promotion parsing)
- **PayPal Sandbox** app credentials 

## API keys and environment

Copy the example env file, then fill in secrets:

```bash
cp .env.example .env
```

### Repo-root `.env` (backend)

| Variable | Required | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | Yes for AI features | OpenAI API key for the agent and promotion extraction |
| `LLM_MODEL` | No (default `gpt-6-luna`) | Model id your OpenAI account can call — change if needed |
| `LLM_TIMEOUT_SECONDS` | No | Per-request timeout to the provider |
| `LLM_REASONING_EFFORT` | No | Leave `none` for models that need tools with reasoning off |
| `PAYPAL_CLIENT_ID` | For checkout | Sandbox REST app client ID |
| `PAYPAL_CLIENT_SECRET` | For checkout | Sandbox REST app secret |
| `PAYPAL_BASE_URL` | No | Must stay `https://api-m.sandbox.paypal.com` (live PayPal is disabled) |
| `APP_ENV` | No | Keep `dev` for local seeding (seed refuses to reset non-dev DBs) |
| `DATABASE_URL` | No | Default SQLite at `backend/stockpilot.db` when you run from `backend/` |
| `FRONTEND_ORIGIN` | No | CORS origin; default `http://localhost:3000` |

### `frontend/.env.local` (frontend)

```bash
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
NEXT_PUBLIC_PAYPAL_CLIENT_ID=<same sandbox client id as PAYPAL_CLIENT_ID>
```

`NEXT_PUBLIC_*` values are visible in the browser. Put only the PayPal **client ID** there — never the secret.

Create a Sandbox REST app at [developer.paypal.com](https://developer.paypal.com/). You will also need a Sandbox buyer account to complete the popup checkout.

## Run locally

### 1. Backend

```bash
cd backend
uv sync
uv run python -m app.seed
uv run uvicorn app.main:app --reload
```

API: [http://localhost:8000](http://localhost:8000)  
Health: [http://localhost:8000/health](http://localhost:8000/health)

Re-seed anytime with `uv run python -m app.seed` (dev only; drops and recreates tables).

### 2. Frontend

In a second terminal:

```bash
cd frontend
npm install
npm run dev
```

App: [http://localhost:3000](http://localhost:3000)


## How AI is used

StockPilot uses the **OpenAI API** directly (no LangChain). The model name comes from `LLM_MODEL`.

1. **Chat / restock agent** — A tool-calling loop reads inventory, sales history, supplier quotes, and promotions, then may draft purchase orders. Tools accept identifiers and quantities only; **prices and totals come from the database and validators**. The agent has **no** tools to approve, cancel, edit, or pay.
2. **Promotion extraction** — Paste messy supplier rebate text; the model proposes structured terms (cases, flavors, dates, amount). Nothing is saved until a human reviews and confirms.
3. **Deterministic core** — Reorder math, pack-size rounding, supplier minimums, stock capacity, budget checks, promotion eligibility, and PO state transitions are ordinary Python services. If the LLM is down, those rules still work on already-saved data.

Without `OPENAI_API_KEY`, the rest of the app can start, but AI endpoints return a clear “not configured” error.

## How PayPal is used

Payment is **Sandbox-only** and human-gated:

1. Merchant reviews a **DRAFT** PO and clicks **Approve** (server checks the approved total matches the stored total).
2. Frontend loads the **PayPal JS SDK** with `NEXT_PUBLIC_PAYPAL_CLIENT_ID`.
3. Backend creates a PayPal order from the **approved PO amount in the database** (never from the browser).
4. Buyer completes the Sandbox popup; backend **captures** and verifies status, amount, currency, and order reference before marking the PO **PAID**.

If PayPal credentials are missing, approve still works; checkout returns “not configured” instead of pretending the order was paid. The agent never talks to PayPal.


## Project layout

```
StockPilot/
├── .env.example          # variable names and safe defaults
├── backend/              # FastAPI + SQLAlchemy + agent + PayPal client
├── frontend/             # Next.js (App Router) UI
```
