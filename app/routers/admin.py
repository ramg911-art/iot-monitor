"""Admin panel - initial setup, password change."""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user_id
from app.database import get_db
from app.models.user import User
from app.services.auth_service import get_password_hash
from app.config import get_settings

router = APIRouter(prefix="/api/admin", tags=["admin"])


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


@router.post("/change-password")
async def change_password(
    data: ChangePasswordRequest,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    """Change admin password."""
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    from app.services.auth_service import verify_password
    if not verify_password(data.old_password, user.password_hash):
        raise HTTPException(status_code=400, detail="Invalid current password")
    user.password_hash = get_password_hash(data.new_password)
    await session.flush()
    return {"status": "ok"}
