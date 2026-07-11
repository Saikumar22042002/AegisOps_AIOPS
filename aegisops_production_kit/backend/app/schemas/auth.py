"""Auth-related Pydantic schemas."""

from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)


class User(BaseModel):
    sub: str
    username: str
    email: str | None = None
    name: str | None = None
    roles: list[str] = Field(default_factory=list)  # kebab-case realm roles
    display_roles: list[str] = Field(default_factory=list)  # UI labels
    can_approve: bool = False
    can_initiate: bool = False
    can_execute: bool = False
    org: str | None = None  # org slug (Keycloak claim or mirror row)
    org_id: str | None = None  # organizations.id the principal resolved to (S0)
    user_id: str | None = None  # users.id mirror row for the principal (S0)


class AuthResponse(BaseModel):
    user: User
