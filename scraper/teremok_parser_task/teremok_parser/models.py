from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from teremok_parser.constants import MISSING
from teremok_parser.text_utils import normalize_text


class ParseStatus(str, Enum):
    SUCCESS = "SUCCESS"
    NETWORK_ERROR = "NETWORK_ERROR"
    HTTP_ERROR = "HTTP_ERROR"
    PARSE_ERROR = "PARSE_ERROR"


class SelectionMode(str, Enum):
    STRICT_NEWEST = "strict_newest"
    QUOTA_COVERAGE = "quota_coverage"


class ListingCandidate(BaseModel):
    id: str
    title: str
    url: str
    city: Optional[str] = None
    preview_category: str
    preview_dt: Optional[datetime] = None


class Listing(BaseModel):
    id: str
    title: str = MISSING
    description: str = MISSING
    price: Optional[Decimal] = None
    currency: Optional[str] = None
    price_raw: str = MISSING

    category: str = MISSING
    category_path: list[str] = Field(default_factory=list)
    source_category: str = MISSING

    city: str = MISSING
    published_at: Optional[datetime] = None
    published_at_raw: str = MISSING
    first_seen_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    url: str
    images: list[str] = Field(default_factory=list)
    seller: str = MISSING
    views: Optional[int] = None
    characteristics: dict[str, str] = Field(default_factory=dict)

    @field_validator("title", "description", "city", "seller", "price_raw", "published_at_raw", mode="before")
    @classmethod
    def _empty_to_missing(cls, v: Any) -> str:
        if v is None:
            return MISSING
        v = str(v).strip()
        return v if v else MISSING

    @property
    def content_hash(self) -> str:
        normalized = "||".join([
            normalize_text(self.title),
            normalize_text(self.description),
            str(self.price) if self.price is not None else "",
            self.currency or "",
            normalize_text(self.city),
            normalize_text(self.seller),
            json.dumps(self.category_path, ensure_ascii=False),
            json.dumps(self.characteristics, ensure_ascii=False, sort_keys=True),
            json.dumps(sorted(self.images), ensure_ascii=False),
        ])
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class ParseResult(BaseModel):
    listing: Optional[Listing] = None
    status: ParseStatus = ParseStatus.SUCCESS
    error_msg: Optional[str] = None

    @model_validator(mode="after")
    def validate_result_invariants(self) -> "ParseResult":
        if self.status == ParseStatus.SUCCESS and self.listing is None:
            raise ValueError("SUCCESS вимагає наявності об'єкта Listing.")
        if self.status != ParseStatus.SUCCESS and not self.error_msg:
            raise ValueError("Помилковий статус вимагає наявності error_msg.")
        return self


Listing.model_rebuild()
ParseResult.model_rebuild()
