from __future__ import annotations

import io
import struct
from pathlib import Path

from PIL import Image, ImageDraw

SIZES = (16, 20, 24, 32, 48, 64, 128, 256)


def render(size: int) -> Image.Image:
    scale = size / 256
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    margin = max(1, round(8 * scale))
    radius = max(3, round(48 * scale))
    draw.rounded_rectangle(
        (margin, margin, size - margin - 1, size - margin - 1),
        radius=radius,
        fill="#0B1F3A",
    )

    cyan = "#35D6E6"
    line_width = max(1, round(11 * scale))
    nodes = ((171, 62), (210, 110), (183, 172))
    center = (145, 124)
    for node in nodes:
        draw.line(
            tuple(round(value * scale) for point in (center, node) for value in point),
            fill=cyan,
            width=line_width,
        )
    node_radius = max(2, round(18 * scale))
    for x, y in nodes:
        x = round(x * scale)
        y = round(y * scale)
        draw.ellipse(
            (x - node_radius, y - node_radius, x + node_radius, y + node_radius),
            fill=cyan,
        )

    pointer = tuple(
        (round(x * scale), round(y * scale))
        for x, y in (
            (55, 45),
            (61, 184),
            (94, 151),
            (122, 211),
            (151, 197),
            (121, 139),
            (168, 132),
        )
    )
    outline = max(1, round(7 * scale))
    draw.polygon(pointer, fill="white", outline="#061426", width=outline)
    return image


def write_ico(path: Path) -> None:
    frames: list[tuple[int, bytes]] = []
    for size in SIZES:
        buffer = io.BytesIO()
        render(size).save(buffer, format="PNG", optimize=True)
        frames.append((size, buffer.getvalue()))

    header_size = 6 + 16 * len(frames)
    offset = header_size
    entries = []
    payload = bytearray()
    for size, png in frames:
        dimension = 0 if size == 256 else size
        entries.append(
            struct.pack(
                "<BBBBHHII",
                dimension,
                dimension,
                0,
                0,
                1,
                32,
                len(png),
                offset,
            )
        )
        payload.extend(png)
        offset += len(png)

    path.write_bytes(
        struct.pack("<HHH", 0, 1, len(frames)) + b"".join(entries) + payload
    )


if __name__ == "__main__":
    write_ico(Path(__file__).with_name("lan-input-share.ico"))
