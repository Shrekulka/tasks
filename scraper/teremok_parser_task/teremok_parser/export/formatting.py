from __future__ import annotations

from teremok_parser.constants import MISSING
from teremok_parser.models import Listing


def listing_to_flat_row(item: Listing) -> dict:
    return {
        "id": item.id,
        "title": item.title,
        "description": item.description,
        "price": float(item.price) if item.price is not None else MISSING,
        "currency": item.currency if item.currency else MISSING,
        "price_raw": item.price_raw,
        "category": item.category,
        "source_category": item.source_category,
        "city": item.city,
        "published_at": item.published_at.isoformat() if item.published_at else MISSING,
        "published_at_raw": item.published_at_raw,
        "first_seen_at": item.first_seen_at.isoformat(),
        "url": item.url,
        "images": "; ".join(item.images) if item.images else MISSING,
        "seller": item.seller,
        "views": item.views if item.views is not None else MISSING,
        "characteristics": (
            "; ".join(f"{k}: {v}" for k, v in item.characteristics.items())
            if item.characteristics else MISSING
        ),
        "content_hash": item.content_hash,
    }
