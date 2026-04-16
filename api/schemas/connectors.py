"""Connector admin endpoint request schemas."""

from typing import Dict

from pydantic import BaseModel, Field


class SaveCredentialsRequest(BaseModel):
    warehouse_id: int = Field(..., gt=0)
    credentials: Dict[str, str] = Field(..., min_length=1)


class TestConnectionRequest(BaseModel):
    warehouse_id: int = Field(..., gt=0)


class DeleteCredentialsRequest(BaseModel):
    warehouse_id: int = Field(..., gt=0)
