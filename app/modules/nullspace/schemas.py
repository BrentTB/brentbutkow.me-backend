from datetime import datetime
from enum import StrEnum

from pydantic import Field, field_validator

from app.camel import CamelModel

_MAX_NAME = 30
# Mirrors the ShipKind union in the game engine.
_SHIP_KINDS = {"fighter", "interceptor", "dreadnought"}


class FlagReason(StrEnum):
    # Why a submission was judged implausible — stored on the row, never served to clients.
    score_exceeds_kills = "score-exceeds-kills"
    kills_exceed_wave = "kills-exceed-wave"
    too_fast_for_wave = "too-fast-for-wave"


class ScoreSubmission(CamelModel):
    # Hard bounds reject malformed/overflowing payloads with a 422; the softer
    # "is this score believable" checks live in the service layer.
    name: str = Field(default="", max_length=_MAX_NAME)
    score: int = Field(ge=0, le=100_000_000)
    kills: int = Field(ge=0, le=10_000_000)
    wave: int = Field(ge=0, le=1_000_000)
    level: int = Field(ge=0, le=1_000_000)
    duration_ms: int = Field(ge=0, le=86_400_000)  # <= 24h; also keeps it inside int4
    ship_kind: str = Field(max_length=_MAX_NAME)
    version: str = Field(min_length=1, max_length=20)
    currency: int = Field(ge=0, le=1_000_000_000)
    space_metal: int = Field(ge=0, le=10_000_000)
    upgrades_purchased: int = Field(ge=0, le=100_000)
    ultimates_owned: int = Field(ge=0, le=1_000)

    @field_validator("name")
    @classmethod
    def _clean_name(cls, value: str) -> str:
        cleaned = value.strip()
        return cleaned if cleaned else "Anonymous"

    @field_validator("ship_kind")
    @classmethod
    def _known_ship(cls, value: str) -> str:
        if value not in _SHIP_KINDS:
            raise ValueError("unknown ship kind")
        return value

    @field_validator("version")
    @classmethod
    def _version_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("version must not be blank")
        return value.strip()


class ScoreResult(CamelModel):
    status: str


class ScoreOut(CamelModel):
    id: int
    created_at: datetime
    name: str
    score: int
    kills: int
    wave: int
    level: int
    ship_kind: str
    version: str
