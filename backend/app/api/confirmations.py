from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import confirmation_service
from app.database import get_db
from app.schemas import ConfirmationCreate, ConfirmationOut
from app.services.draft_consistency import DraftConsistencyError

router = APIRouter()


@router.post("/confirmations", response_model=ConfirmationOut)
def create_confirmation(data: ConfirmationCreate, db: Session = Depends(get_db)):
    return confirmation_service.create(db, data)


@router.get("/confirmations/{confirmation_id}", response_model=ConfirmationOut)
def get_confirmation(confirmation_id: str, db: Session = Depends(get_db)):
    item = confirmation_service.get(db, confirmation_id)
    if not item:
        raise HTTPException(status_code=404, detail="Confirmation not found")
    return item


@router.post("/confirmations/{confirmation_id}/confirm", response_model=ConfirmationOut)
def confirm_action(confirmation_id: str, db: Session = Depends(get_db)):
    try:
        return confirmation_service.confirm(db, confirmation_id)
    except DraftConsistencyError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "message": str(exc),
                "issues": [i.to_dict() for i in exc.issues],
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/confirmations/{confirmation_id}/cancel", response_model=ConfirmationOut)
def cancel_action(confirmation_id: str, db: Session = Depends(get_db)):
    try:
        return confirmation_service.cancel(db, confirmation_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
