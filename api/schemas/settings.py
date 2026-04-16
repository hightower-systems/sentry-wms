"""Settings request schemas."""

from typing import Dict

from pydantic import BaseModel, Field


class UpdateSettingsRequest(BaseModel):
    settings: Dict[str, str] = Field(..., min_length=1)
