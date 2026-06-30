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
    org: str | None = None


class AuthResponse(BaseModel):
    user: User
