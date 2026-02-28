from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.database import get_db
from app.models import Case, Demand, Supply, AllocationRun
from app.schemas import CaseCreate, CaseUpdate, CaseResponse, CaseDetailResponse
from app.services.csv_import import import_case_from_folder

router = APIRouter(prefix="/cases", tags=["Case"])


@router.post("", response_model=CaseResponse)
def create_case(body: CaseCreate, db: Session = Depends(get_db)):
    c = Case(name=body.name)
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


@router.get("", response_model=List[CaseResponse])
def list_cases(db: Session = Depends(get_db)):
    return db.query(Case).order_by(Case.created_at.desc()).all()


@router.get("/{case_id}", response_model=CaseDetailResponse)
def get_case(case_id: int, db: Session = Depends(get_db)):
    c = db.query(Case).filter(Case.id == case_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Case not found")
    demand_count = db.query(func.count(Demand.id)).filter(Demand.case_id == case_id).scalar() or 0
    supply_count = db.query(func.count(Supply.id)).filter(Supply.case_id == case_id).scalar() or 0
    run_count = db.query(func.count(AllocationRun.id)).filter(AllocationRun.case_id == case_id).scalar() or 0
    return CaseDetailResponse(
        id=c.id,
        name=c.name,
        created_at=c.created_at,
        demand_count=demand_count,
        supply_count=supply_count,
        run_count=run_count,
    )


@router.put("/{case_id}", response_model=CaseResponse)
def update_case(case_id: int, body: CaseUpdate, db: Session = Depends(get_db)):
    c = db.query(Case).filter(Case.id == case_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Case not found")
    if body.name is not None:
        c.name = body.name
    db.commit()
    db.refresh(c)
    return c


@router.delete("/{case_id}", status_code=204)
def delete_case(case_id: int, db: Session = Depends(get_db)):
    c = db.query(Case).filter(Case.id == case_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Case not found")
    db.delete(c)
    db.commit()
    return None


def _csv_folder_path(folder_path: str | None) -> Path:
    from app.config import settings
    if folder_path:
        p = Path(folder_path)
        if p.is_absolute():
            return p
        return Path.cwd().resolve() / p
    base = Path.cwd().resolve()
    candidate = base / settings.csv_root_path
    if candidate.is_dir():
        return candidate
    # When running from backend/, csv is at ../csv
    candidate = base.parent / settings.csv_root_path
    if candidate.is_dir():
        return candidate
    return base / settings.csv_root_path


@router.post("/{case_id}/import-csv", status_code=200)
def import_csv(
    case_id: int,
    db: Session = Depends(get_db),
    folder_path: str | None = None,
):
    """Import CSV set from folder. Use query param folder_path to override default (e.g. 'csv' or absolute path)."""
    c = db.query(Case).filter(Case.id == case_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Case not found")
    folder = _csv_folder_path(folder_path)
    if not folder.is_dir():
        raise HTTPException(status_code=400, detail=f"Folder not found: {folder}")
    import_case_from_folder(db, case_id, folder)
    return {"status": "ok", "case_id": case_id}
