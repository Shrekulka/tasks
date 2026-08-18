from __future__ import annotations

import json
from pathlib import Path

from teremok_parser.models import Listing


def export_json(listings: list[Listing], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    data = []
    for item in listings:
        data.append({
            "id": item.id,
            "title": item.title,
            "description": item.description,
            "price": {
                "amount": float(item.price) if item.price is not None else None,
                "currency": item.currency,
                "raw": item.price_raw,
            },
            "category": {
                "source_category": item.source_category,
                "full_path": item.category_path if item.category_path else None,
                "raw": item.category,
            },
            "city": item.city,
            "published_at": item.published_at.isoformat() if item.published_at else None,
            "published_at_raw": item.published_at_raw,
            "url": item.url,
            "images": item.images,
            "seller": item.seller,
            "views": item.views,
            "characteristics": item.characteristics,
            "metadata": {
                "source_category": item.source_category,
                "first_seen_at": item.first_seen_at.isoformat(),
                "content_hash": item.content_hash,
            },
        })
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
