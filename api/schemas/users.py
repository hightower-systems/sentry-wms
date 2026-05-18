"""User request schemas."""

from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

VALID_ROLES = ("ADMIN", "USER")


class UpdateUserPagePermissionsRequest(BaseModel):
    """avid-overhaul-mk1 P6.1: replaces a user's full per-page grant
    set in one PUT. The handler validates each key against
    constants.ALL_PAGE_KEYS so a typo lands as a clean 400 rather
    than a silent grant that the sidebar will never honor."""

    page_keys: List[str] = Field(default_factory=list, max_length=200)


class CreateUserRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=150)
    password: str = Field(..., min_length=1, max_length=256)
    full_name: str = Field(..., min_length=1, max_length=256)
    role: str = Field(..., min_length=1, max_length=32)
    warehouse_ids: List[int] = Field(default_factory=list)
    warehouse_id: Optional[int] = Field(None, gt=0)
    allowed_functions: List[str] = Field(default_factory=list)

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        if v not in VALID_ROLES:
            raise ValueError(f"role must be one of: {', '.join(VALID_ROLES)}")
        return v


class UpdateUserRequest(BaseModel):
    full_name: Optional[str] = Field(None, min_length=1, max_length=256)
    role: Optional[str] = Field(None, min_length=1, max_length=32)
    warehouse_id: Optional[int] = Field(None, gt=0)
    warehouse_ids: Optional[List[int]] = None
    allowed_functions: Optional[List[str]] = None
    is_active: Optional[bool] = None
    password: Optional[str] = Field(None, min_length=1, max_length=256)

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in VALID_ROLES:
            raise ValueError(f"role must be one of: {', '.join(VALID_ROLES)}")
        return v
