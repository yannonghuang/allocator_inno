from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.database import engine, Base
from app.api.cases import router as cases_router
from app.api.allocate import router as allocate_router
from app.api.overrides import router as overrides_router
from app.api.explanations import router as explanations_router
from app.api.pegging import router as pegging_router
from app.api.views import router as views_router

app = FastAPI(title="Supply-Demand Allocator API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(cases_router)
app.include_router(allocate_router)
app.include_router(overrides_router)
app.include_router(explanations_router)
app.include_router(pegging_router)
app.include_router(views_router)


def _migrate_schema(conn):
    """Add missing columns and rename old table so existing DBs work after code changes."""
    from sqlalchemy import text

    # Add method_make.lead_time if missing (e.g. DB created before this column existed)
    conn.execute(text(
        "ALTER TABLE method_make ADD COLUMN IF NOT EXISTS lead_time INTEGER"
    ))
    # Add allocation_action.output_period if missing
    conn.execute(text(
        "ALTER TABLE allocation_action ADD COLUMN IF NOT EXISTS output_period INTEGER"
    ))
    # Add allocation_action.edge_type if missing ("make" | "move")
    conn.execute(text(
        "ALTER TABLE allocation_action ADD COLUMN IF NOT EXISTS edge_type VARCHAR(32)"
    ))
    # Add allocation_action.scarcity_rank for replay in scarcity order
    conn.execute(text(
        "ALTER TABLE allocation_action ADD COLUMN IF NOT EXISTS scarcity_rank INTEGER"
    ))
    # Add allocation_action.req_rates for correct supply-view consumption (need_i = qty/rate_i)
    conn.execute(text(
        "ALTER TABLE allocation_action ADD COLUMN IF NOT EXISTS req_rates JSON"
    ))
    # Rename old transportation table to method_move if it still exists
    r = conn.execute(text(
        "SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'transportation'"
    ))
    if r.scalar() is not None:
        conn.execute(text("ALTER TABLE transportation RENAME TO method_move"))
    conn.commit()


def _create_database_if_missing():
    """Create the app database if it doesn't exist (e.g. after a full volume purge)."""
    from urllib.parse import urlparse, urlunparse
    from sqlalchemy import create_engine, text

    from app.config import settings

    url = urlparse(settings.database_url)
    db_name = url.path.lstrip("/")
    if not db_name:
        return
    postgres_url = urlunparse((url.scheme, url.netloc, "postgres", url.params, url.query, url.fragment))
    eng = create_engine(postgres_url, isolation_level="AUTOCOMMIT")
    with eng.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    eng.dispose()


@app.on_event("startup")
def startup():
    import time
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    for attempt in range(30):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            break
        except OperationalError as e:
            if "does not exist" in str(e.orig):
                _create_database_if_missing()
                break
            if attempt == 29:
                raise
        except Exception:
            if attempt == 29:
                raise
        time.sleep(1)
    Base.metadata.create_all(bind=engine)
    with engine.connect() as conn:
        _migrate_schema(conn)


@app.get("/health")
def health():
    return {"status": "ok"}
