from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.modules.recalls.schemas import RecallCategory, RecallCountry
from app.subscriptions.models import SEVERITY_ORDER

_VALID_CATEGORIES = {c.value for c in RecallCategory}
_VALID_COUNTRIES = {c.value for c in RecallCountry}

CountriesField = Annotated[
    list[str],
    Field(min_length=1, description=f"At least one of: {', '.join(sorted(_VALID_COUNTRIES))}"),
]


class SubscriptionCreate(BaseModel):
    email: EmailStr
    countries: CountriesField
    entities: list[Annotated[str, Field(max_length=100)]] = Field(default=[], max_length=50)
    companies: list[Annotated[str, Field(max_length=200)]] = Field(default=[], max_length=50)
    categories: list[str] = []
    min_severity: str | None = None

    @field_validator("countries", mode="before")
    @classmethod
    def validate_countries(cls, v: object) -> object:
        if isinstance(v, list):
            for item in v:
                if item not in _VALID_COUNTRIES:
                    raise ValueError("invalid_country")
        return v

    @field_validator("min_severity", mode="before")
    @classmethod
    def validate_min_severity(cls, v: object) -> object:
        if v is not None and v not in SEVERITY_ORDER:
            raise ValueError("invalid_severity")
        return v

    @field_validator("categories", mode="before")
    @classmethod
    def validate_categories(cls, v: object) -> object:
        if isinstance(v, list):
            for item in v:
                if item not in _VALID_CATEGORIES:
                    raise ValueError("invalid_category")
        return v


class SubscriptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    status: str
    countries: list[str]
    entities: list[str]
    companies: list[str]
    categories: list[str]
    min_severity: str | None


class SubscriptionPatch(BaseModel):
    countries: CountriesField | None = None
    entities: list[Annotated[str, Field(max_length=100)]] | None = None
    companies: list[Annotated[str, Field(max_length=200)]] | None = None
    categories: list[str] | None = None
    min_severity: str | None = None

    @field_validator("countries", mode="before")
    @classmethod
    def validate_countries(cls, v: object) -> object:
        if isinstance(v, list):
            for item in v:
                if item not in _VALID_COUNTRIES:
                    raise ValueError("invalid_country")
        return v

    @field_validator("min_severity", mode="before")
    @classmethod
    def validate_min_severity(cls, v: object) -> object:
        if v is not None and v not in SEVERITY_ORDER:
            raise ValueError("invalid_severity")
        return v

    @field_validator("categories", mode="before")
    @classmethod
    def validate_categories(cls, v: object) -> object:
        if isinstance(v, list):
            for item in v:
                if item not in _VALID_CATEGORIES:
                    raise ValueError("invalid_category")
        return v
