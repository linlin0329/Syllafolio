"""Render the Syllafolio Windows icon at native shell sizes (requires Pillow)."""
from pathlib import Path

from PIL import Image, ImageDraw


def main():
    size = 512
    icon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    pen = ImageDraw.Draw(icon)
    pen.rounded_rectangle((12, 12, 500, 500), radius=104, fill="#246B77")
    # Two matching syllable envelopes, legible even at 16 px.
    for center_x, half_height in zip((88, 144, 200, 256, 312, 368, 424),
                                     (28, 87, 145, 58, 112, 78, 28)):
        pen.rounded_rectangle((center_x - 15, 256 - half_height, center_x + 15, 256 + half_height),
                              radius=15, fill="#FFFFFF")
    icon.save(Path(__file__).with_name("Syllafolio.ico"), format="ICO",
              sizes=[(n, n) for n in (16, 24, 32, 48, 64, 128, 256)])


if __name__ == "__main__":
    main()
