from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Case, ManualOverride
from app.schemas import ManualOverrideCreate, ManualOverrideResponse

router = APIRouter(prefix="/cases", tags=["Overrides"])


@router.get("/{case_id}/overrides", response_model=List[ManualOverrideResponse])
def list_overrides(case_id: int, db: Session = Depends(get_db)):
    c = db.query(Case).filter(Case.id == case_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Case not found")
    return db.query(ManualOverride).filter(ManualOverride.case_id == case_id).all()


@router.post("/{case_id}/overrides", response_model=ManualOverrideResponse)
def add_override(case_id: int, body: ManualOverrideCreate, db: Session = Depends(get_db)):
    c = db.query(Case).filter(Case.id == case_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Case not found")
    o = ManualOverride(
        case_id=case_id,
        entity_type=body.entity_type,
        entity_key=body.entity_key,
        payload=body.payload,
    )
    db.add(o)
    db.commit()
    db.refresh(o)
    return o


@router.put("/{case_id}/overrides")
def replace_overrides(case_id: int, overrides: List[ManualOverrideCreate], db: Session = Depends(get_db)):
    c = db.query(Case).filter(Case.id == case_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Case not found")
    db.query(ManualOverride).filter(ManualOverride.case_id == case_id).delete()
    for body in overrides:
        o = ManualOverride(
            case_id=case_id,
            entity_type=body.entity_type,
            entity_key=body.entity_key,
            payload=body.payload,
        )
        db.add(o)
    db.commit()
    return {"status": "ok", "count": len(overrides)}


@router.delete("/{case_id}/overrides/{override_id}", status_code=204)
def delete_override(case_id: int, override_id: int, db: Session = Depends(get_db)):
    o = db.query(ManualOverride).filter(ManualOverride.id == override_id, ManualOverride.case_id == case_id).first()
    if not o:
        raise HTTPException(status_code=404, detail="Override not found")
    db.delete(o)
    db.commit()
    return None
