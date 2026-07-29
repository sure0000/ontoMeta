"""主体（Principal）管理：创建、改角色、启停、轮换 Token。

Token 明文**仅在创建与轮换时返回一次**，库内只存 SHA-256 哈希与前缀——
与 External App Key 的既有做法一致（见 ``services/external_api.py``）。
"""

from __future__ import annotations

import secrets

from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.auth import api_key_prefix, hash_api_key
from app.config import settings
from app.models.principal import Principal, Role


def _generate_token() -> str:
    return f"om_pr_{secrets.token_urlsafe(32)}"


class PrincipalService:
    def list_principals(self, db: Session) -> list[Principal]:
        return db.query(Principal).order_by(desc(Principal.created_at)).all()

    def get(self, db: Session, principal_id: str) -> Principal | None:
        return db.get(Principal, principal_id)

    def create(self, db: Session, *, name: str, role: str) -> tuple[Principal, str]:
        """返回 (主体, Token 明文)。明文此后不可再取。"""
        self._validate_role(role)
        token = _generate_token()
        principal = Principal(
            name=name,
            role=role,
            token_hash=hash_api_key(token, settings.api_key_hash_pepper),
            token_prefix=api_key_prefix(token),
        )
        db.add(principal)
        db.commit()
        db.refresh(principal)
        return principal, token

    def update(
        self,
        db: Session,
        principal_id: str,
        *,
        name: str | None = None,
        role: str | None = None,
        active: bool | None = None,
    ) -> Principal | None:
        principal = db.get(Principal, principal_id)
        if principal is None:
            return None
        if name is not None:
            principal.name = name
        if role is not None:
            self._validate_role(role)
            principal.role = role
        if active is not None:
            principal.active = active
        db.commit()
        db.refresh(principal)
        return principal

    def rotate_token(self, db: Session, principal_id: str) -> tuple[Principal, str] | None:
        principal = db.get(Principal, principal_id)
        if principal is None:
            return None
        token = _generate_token()
        principal.token_hash = hash_api_key(token, settings.api_key_hash_pepper)
        principal.token_prefix = api_key_prefix(token)
        db.commit()
        db.refresh(principal)
        return principal, token

    def delete(self, db: Session, principal_id: str) -> bool:
        principal = db.get(Principal, principal_id)
        if principal is None:
            return False
        db.delete(principal)
        db.commit()
        return True

    @staticmethod
    def _validate_role(role: str) -> None:
        if role not in {r.value for r in Role}:
            raise ValueError(
                f"未知角色 {role}，可选：{', '.join(r.value for r in Role)}"
            )
