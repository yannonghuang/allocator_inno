# Supply–Demand Allocator

Allocate supplies to competing demands with automatic allocation, manual overrides, explainability, and pegging views.

## Stack

- **Frontend**: Next.js 14 (App Router), TypeScript
- **Backend**: FastAPI, Python 3.11+
- **Database**: Postgres 15+
- **Deployment**: Docker Compose

## Quick start (local, no Docker)

### Backend

1. Create a Postgres database (e.g. `createdb allocator`).
2. From project root:
   ```bash
   cd backend
   pip install -r requirements.txt
   export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/allocator
   uvicorn app.main:app --reload --app-dir .
   ```
   Or with project venv: `cd backend && PYTHONPATH=. ../.venv/bin/uvicorn app.main:app --reload`

3. API: http://localhost:8000  
   Docs: http://localhost:8000/docs

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:3000

### Load sample data

1. Create a case (e.g. "My case") in the UI.
2. Click **Import CSV** for that case. The backend reads from the `csv/` folder (relative to backend cwd or project root).
3. Click **Run allocation** on the case detail page.
4. Open **Explainability & Pegging** for a run to see split explanations and the pegging graph.

## Docker Compose

**Default is dev mode** (hot reload). Plain `docker compose up --build` runs with local source mounted.

```bash
docker compose up --build
```

- Backend runs `uvicorn ... --reload`; edits under `backend/` reload automatically.
- Frontend runs `npm run dev`; edits under `frontend/` reload automatically.
- `csv/` is mounted at `/app/csv` in the backend container.

### Prod

No source mounts; uses production Dockerfiles and built images.

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up --build
```

### Ports and DB

- Frontend: http://localhost:3000  
- Backend: http://localhost:8000  
- Postgres: localhost:5432 (user `postgres`, password `postgres`, db `allocator`)

After creating a case, use **Import CSV** with no folder path so it uses the default `/app/csv`.

## API overview

- **Cases**: `GET/POST /cases`, `GET/PUT/DELETE /cases/{id}`, `POST /cases/{id}/import-csv`
- **Allocation**: `POST /cases/{id}/allocate`, `GET /cases/{id}/runs`, `GET /cases/{id}/runs/{runId}`
- **Overrides**: `GET/POST /cases/{id}/overrides`, `PUT /cases/{id}/overrides`, `DELETE /cases/{id}/overrides/{overrideId}`
- **Explainability**: `GET /cases/{id}/runs/{runId}/explanations?supply_id=...`
- **Pegging**: `GET /cases/{id}/runs/{runId}/pegging?direction=demand-to-supply&demand_id=...` or `direction=supply-to-demand&supply_id=...`

## Tests

```bash
cd backend
PYTHONPATH=. python -m pytest tests/ -v
```

## Data (CSV)

Place CSVs in `csv/` with the names and columns described in `spec.md`: `bom`, `customer`, `demand`, `location`, `method_buy`, `method_make`, `method_move`, `product`, `productlocation`, `supply`, `vendor`.
