from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.core.database import get_db, settings
from app.models.user import Permission, User, UserRole

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


def _normalize_permission_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (Permission, UserRole)):
        return value.value
    if isinstance(value, str):
        normalized = value.strip()
        return normalized or None
    if hasattr(value, "value"):
        normalized = str(value.value).strip()
        return normalized or None
    normalized = str(value).strip()
    return normalized or None


def _parse_permission(value: Any) -> Permission | None:
    if value is None:
        return None
    if isinstance(value, Permission):
        return value
    if isinstance(value, str):
        try:
            return Permission(value)
        except ValueError:
            return None
    if hasattr(value, "value"):
        try:
            return Permission(str(value.value))
        except ValueError:
            return None
    return None


def normalize_permissions(raw_permissions: Any) -> list[Permission]:
    permissions: list[Permission] = []
    seen: set[str] = set()
    for value in raw_permissions or []:
        permission = _parse_permission(value)
        if permission is None:
            continue
        permission_name = permission.value
        if permission_name not in seen:
            seen.add(permission_name)
            permissions.append(permission)
    return permissions


def _role_matches(role: Any, expected: UserRole) -> bool:
    if isinstance(role, UserRole):
        return role == expected
    return str(role) == expected.value


def require_admin(user: User | None) -> bool:
    if not user:
        return False
    if getattr(user, "is_active", True) is False:
        return False
    return _role_matches(getattr(user, "role", None), UserRole.ADMIN)


def require_permission(user: User | None, permission: Permission | str | None = None) -> bool:
    if isinstance(user, (Permission, str)) and permission is None:
        permission_name = user

        def _dependency(current_user: User = Depends(get_current_user)) -> User:
            if not require_permission(current_user, permission_name):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Permission required: {_normalize_permission_value(permission_name) or str(permission_name)}",
                )
            return current_user

        return _dependency

    if not user:
        return False
    if getattr(user, "is_active", True) is False:
        return False
    if require_admin(user):
        return True
    if not _role_matches(getattr(user, "role", None), UserRole.STAFF):
        return False
    permission_name = _normalize_permission_value(permission)
    if not permission_name:
        return False
    user_permissions = {item.value for item in normalize_permissions(getattr(user, "permissions", []))}
    return permission_name in user_permissions


def get_user_permissions(user: User | None) -> list[Permission]:
    if not user or getattr(user, "is_active", True) is False:
        return []
    if not _role_matches(getattr(user, "role", None), UserRole.STAFF):
        return []
    return normalize_permissions(getattr(user, "permissions", []))


def ensure_permission(db: Session | None, user: User | None, permission: Permission | str) -> None:
    if not require_permission(user, permission):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Permission required: {_normalize_permission_value(permission) or str(permission)}",
        )


def hash_password(password: str) -> str:
    import hashlib
    import secrets

    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200_000)
    return f"pbkdf2_sha256${salt}${digest.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    import hashlib
    import hmac

    if not password_hash or "$" not in password_hash:
        return False

    algo, salt, expected = password_hash.split("$", 2)
    if algo != "pbkdf2_sha256":
        return False

    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200_000)
    return hmac.compare_digest(actual.hex(), expected)


def create_access_token(user_id: int) -> str:
    expires = datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {"sub": str(user_id), "exp": int(expires.timestamp())}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    if not token:
        raise credentials_exception

    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        user_id = payload.get("sub")
        if user_id is None:
            raise credentials_exception
    except jwt.PyJWTError as exc:
        raise credentials_exception from exc

    user = db.query(User).filter(User.id == int(user_id)).first()
    if user is None:
        raise credentials_exception
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account is inactive",
        )
    return user


def require_staff(user: User | None) -> bool:
    if not user:
        return False
    if getattr(user, "is_active", True) is False:
        return False
    return _role_matches(getattr(user, "role", None), UserRole.STAFF) or require_admin(user)


def permission_required(permission: Permission | str):
    def _dependency(current_user: User = Depends(get_current_user)) -> User:
        if not require_permission(current_user, permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission required: {_normalize_permission_value(permission) or str(permission)}",
            )
        return current_user

    return _dependency
