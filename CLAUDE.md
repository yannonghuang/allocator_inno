# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

### Backend (FastAPI)
```bash
cd backend
pip install -r requirements.txt
export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/allocator
uvicorn app.main:app --reload --app-dir .
# Or with project venv:
PYTHONPATH=. ../.venv/bin/uvicorn app.main:app --reload
```

### Frontend (Next.js)
```bash
cd frontend
npm install
npm run dev
```

### Tests
```bash
cd backend
PYTHONPATH=. python -m pytest tests/ -v
# Single test file:
PYTHONPATH=. python -m pytest tests/test_allocation_engine.py -v
```

### Docker (Development — hot reload)
```bash
docker compose up --build
```

### Docker (Production)
```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up --build
```

## Architecture

This is a **supply-demand allocation engine** — it allocates scarce inventory to competing customer demands, with explainability and pegging views.

### Stack
- **Backend**: FastAPI + SQLAlchemy (PostgreSQL), Python 3.11+
- **Frontend**: Next.js 14 App Router, TypeScript
- **Database**: PostgreSQL (pgvector:pg17 in Docker)
- **AI**: OpenAI integration in `planning_copilot.py` for natural-language intent parsing (`OPENAI_API_KEY` in `.env`)

### Data Model (Inventory Graph DAG)
The core domain is a directed acyclic graph:
- **Nodes**: Inventory = (product, location, quantity, time)
- **Supplies/Demands**: Terminal nodes
- **Edges (Methods)**: Transform inventory across space/time/form
  - `method_move`: Transport between locations
  - `method_make`: Manufacture using BOM (bill of materials)
  - `method_buy`: Purchase from vendor

**BOM logic**: AND within a requirement set (all components needed), OR across alternative groups (interchangeable substitutes). This drives the allocation algorithm's complexity.

### Key Services
- **`allocation_engine.py`** (35KB): Core allocation algorithm. Processes components in ascending scarcity order (scarcer components first). Scarcity score = `supply(component) / total_cap(component)`. Splits available quantity across demands proportional to `target_weight` (customer priority × demand quantity).
- **`planning_engine.py`** (54KB): Translates demands into work orders by traversing the inventory graph backward from demands to sources.
- **`csv_import.py`**: Parses 11 CSV tables (bom, customer, demand, location, method_buy, method_make, method_move, product, productlocation, supply, vendor) into the database.
- **`planning_copilot.py`**: OpenAI-powered natural language → allocation intent parsing.

### API Routes (`backend/app/api/`)
- `cases.py`: Case CRUD + CSV import trigger
- `allocate.py`: Start allocation job (async/background), poll run status
- `explanations.py`: Why a supply was split among competing demands
- `pegging.py`: Demand→supply (backward) and supply→demand (forward) graph traversal
- `views.py`: UI-ready views — allocation table, supply consumption, feasible demands
- `overrides.py`: Manual override management

### Frontend Structure (`frontend/app/`)
- `page.tsx`: Home — list/create/delete cases, import CSV
- `cases/[id]/page.tsx`: Case detail — view demands/supplies, trigger allocation, list runs
- `cases/[id]/runs/[runId]/page.tsx`: Run results — allocation table, explanations, pegging tree
- `components/SortFilterTable.tsx`: Reusable sortable/filterable table
- `components/PeggingTree.tsx`: Interactive demand-supply graph visualization
- `lib/api.ts`: All TypeScript types and fetch wrappers for backend endpoints

### Data Flow
1. Create a case → Import CSV files → triggers `csv_import.py` to load all 11 tables
2. Run allocation → `planning_engine.py` builds the graph → `allocation_engine.py` computes allocations → stores `AllocationRun` + `AllocationActions`
3. UI fetches views/explanations/pegging for a run → read-only queries over stored results

### Database Schema
Core transactional tables: `cases`, `demands`, `supplies`, `products`, `locations`, `customers`, `vendors`, `boms`, `method_make`, `method_move`, `method_buy`, `product_locations`  
Results tables: `allocation_runs`, `allocation_actions`, `manual_overrides`

### Sample Data
CSV files in `csv/` directory. After starting the backend, create a case in the UI and click **Import CSV** (no path needed — reads from `csv/` by default).
