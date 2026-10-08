# python_style_extractor/tests/test_png_alpha_sampling.py

"""Тесты сэмплинга цветов из PNG с прозрачностью (`sample_dominant_colors_from_png`)."""

from pathlib import Path

from PIL import Image

from scripts.color_resolution import sample_dominant_colors_from_png


def test_transparent_pixels_do_not_become_black(tmp_path: Path) -> None:
    """Прозрачные пиксели `(0, 0, 0, 0)` не должны превращаться в чёрный цвет.

    `convert("RGB")` отбросил бы альфа-канал, и чёрный победил бы в подсчёте
    частот. Поэтому такие пиксели исключаются по `min_alpha`.
    """
    png_path = tmp_path / "ref.png"
    img = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    for y in range(100):
        for x in range(50, 100):
            img.putpixel((x, y), (2, 1, 13, 255))
    img.save(png_path)

    colors = sample_dominant_colors_from_png(png_path, k=1, resize_dim=40, min_alpha=0.5)
    assert colors == ["rgba(2, 1, 13, 1.0)"]


def test_fully_transparent_image_returns_empty_list(tmp_path: Path) -> None:
    """Полностью прозрачное изображение даёт пустой список: надёжных пикселей нет."""
    png_path = tmp_path / "empty.png"
    Image.new("RGBA", (50, 50), (0, 0, 0, 0)).save(png_path)

    colors = sample_dominant_colors_from_png(png_path, k=1, resize_dim=20, min_alpha=0.5)
    assert colors == []


def test_composite_bg_blends_semi_transparent_pixels(tmp_path: Path) -> None:
    """Полупрозрачный белый пиксель поверх чёрного `composite_bg` даёт серый, а не белый.

    Это подтверждает альфа-композитинг (alpha ≈ 0.6).
    """
    png_path = tmp_path / "semi.png"
    Image.new("RGBA", (10, 10), (255, 255, 255, 153)).save(png_path)  # alpha=153/255≈0.6

    colors = sample_dominant_colors_from_png(
        png_path, k=1, resize_dim=10, min_alpha=0.5, composite_bg=(0, 0, 0)
    )
    assert colors[0].startswith("rgba(153, 153, 153")
