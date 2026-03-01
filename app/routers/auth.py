"""Auth endpoints - login, JWT."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import User
from app.services.auth_service import create_access_token, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


@router.post("/login", response_model=TokenResponse)
async def login(data: LoginRequest, session: AsyncSession = Depends(get_db)):
    result = await session.execute(select(User).where(User.username == data.username))
    user = result.scalars().first()
    if not user or not verify_password(data.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not user.is_active:
        raise HTTPException(status_code=401, detail="Account disabled")
    token = create_access_token(data={"sub": str(user.id)})
    return TokenResponse(access_token=token)
