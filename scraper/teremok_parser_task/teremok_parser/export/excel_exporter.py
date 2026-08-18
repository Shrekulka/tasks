from __future__ import annotations

from pathlib import Path

import pandas as pd

from teremok_parser.export.formatting import listing_to_flat_row
from teremok_parser.models import Listing


def export_excel(listings: list[Listing], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame([listing_to_flat_row(item) for item in listings])
    df.to_excel(output_path, index=False)
