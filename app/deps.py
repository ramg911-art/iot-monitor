"""FastAPI dependencies - auth, db."""
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer, OAuth2PasswordBearer

from app.database import get_db
from app.services.auth_service import decode_token
from sqlalchemy.ext.asyncio import AsyncSession

security = HTTPBearer(auto_error=False)


async def get_current_user_id(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> int:
    """Extract and validate JWT, return user_id. Single-user system uses user_id=1."""
    token = None
    if credentials:
        token = credentials.credentials
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    payload = decode_token(token)
    if not payload or "sub" not in payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    try:
        return int(payload["sub"])
    except (ValueError, TypeError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
