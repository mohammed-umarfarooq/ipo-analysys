"""Generate the launcher's Windows icon — no third-party imaging library needed.

Pillow is not installed in this project's venv and adding it to pull one 30 KB file
out of it would be a poor trade, so this writes the ``.ico`` container by hand. The
format is old and small: a 6-byte directory header, one 16-byte entry per size, then
one 40-byte BITMAPINFOHEADER + bottom-up BGRA pixels per image.

Every image is a 32-bit DIB rather than an embedded PNG. PNG-in-ICO is legal from
Vista onward and would make the file ~20x smaller, but Explorer, the taskbar, the
Start menu and the alt-tab switcher have each historically disagreed about which
sizes may use it. A plain DIB is understood everywhere, and 270 KB on disk does not
matter for a file that is read once per shell refresh.

Drawing is done by supersampling — each output pixel averages an SS x SS grid of
coverage tests — because a 16 px icon with hard edges reads as mud in the taskbar.

Two icons come out of it: the app itself (ascending bars) and its Stop shortcut (a
stop square). Two identical icons sitting next to each other on a desktop is a trap.

Run: uv run python launcher/make_icon.py
"""

from __future__ import annotations

import struct
from pathlib import Path

#: Set from the command line; read by :func:`_shade`, which is called per subpixel and
#: so is kept free of parameters it would only pass straight through.
MODE = "app"

#: Sizes Windows actually asks for: taskbar/titlebar, Explorer small and medium,
#: and the 256 px master used for Jump Lists, alt-tab and "Extra large icons".
SIZES = (16, 32, 48, 256)

BACKGROUND = (0x0B, 0x12, 0x20)  # slate-950, the dashboard's own page colour
BASELINE = (0x47, 0x55, 0x69)  # slate-600
#: Three ascending bars — the cashflow matrix's shape, in its accent colours.
BARS = (
    (0.30, (0x38, 0xBD, 0xF8)),  # sky-400
    (0.46, (0xFB, 0xBF, 0x24)),  # amber-400
    (0.62, (0x34, 0xD3, 0x99)),  # emerald-400
)

CORNER = 0.22  # background corner radius, as a fraction of the icon's width
BAR_W = 0.17
BAR_GAP = 0.075
BAR_R = 0.04
Y_BASE = 0.80  # where the bars stand
BASELINE_H = 0.035

#: The Stop shortcut: one filled square, the universal "stop" mark. A power symbol
#: reads better at 256 px but turns into a grey blob at 16.
STOP_COLOUR = (0xF8, 0x71, 0x71)  # rose-400
STOP_INSET = 0.30
STOP_R = 0.06


def _in_rounded_rect(
    x: float, y: float, x0: float, y0: float, x1: float, y1: float, r: float
) -> bool:
    """Is the point inside a rounded rectangle?

    Clamp the point into the rectangle deflated by ``r``; if the clamped point is
    within ``r``, the point is inside. Handles the straight edges and all four
    corners without special-casing either.
    """
    r = min(r, (x1 - x0) / 2, (y1 - y0) / 2)
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    dx, dy = x - cx, y - cy
    return dx * dx + dy * dy <= r * r


def _shade(x: float, y: float) -> tuple[int, int, int, int] | None:
    """The colour at a point in the unit square, or None for transparent."""
    if not _in_rounded_rect(x, y, 0.0, 0.0, 1.0, 1.0, CORNER):
        return None

    if MODE == "stop":
        if _in_rounded_rect(
            x, y, STOP_INSET, STOP_INSET, 1 - STOP_INSET, 1 - STOP_INSET, STOP_R
        ):
            return (*STOP_COLOUR, 255)
        return (*BACKGROUND, 255)

    span = 3 * BAR_W + 2 * BAR_GAP
    left = (1.0 - span) / 2

    for index, (height, colour) in enumerate(BARS):
        bx0 = left + index * (BAR_W + BAR_GAP)
        if _in_rounded_rect(x, y, bx0, Y_BASE - height, bx0 + BAR_W, Y_BASE, BAR_R):
            return (*colour, 255)

    if left <= x <= left + span and Y_BASE <= y <= Y_BASE + BASELINE_H:
        return (*BASELINE, 255)

    return (*BACKGROUND, 255)


def render(size: int, samples: int) -> list[tuple[int, int, int, int]]:
    """One image, top-down, as straight-alpha RGBA tuples."""
    pixels: list[tuple[int, int, int, int]] = []
    step = 1.0 / (size * samples)
    for py in range(size):
        for px in range(size):
            r = g = b = a = 0.0
            for sy in range(samples):
                y = (py * samples + sy + 0.5) * step
                for sx in range(samples):
                    x = (px * samples + sx + 0.5) * step
                    hit = _shade(x, y)
                    if hit is None:
                        continue
                    # Premultiply so partially-covered edges blend, then divide back
                    # out below. Averaging straight alpha would darken the corners.
                    r += hit[0]
                    g += hit[1]
                    b += hit[2]
                    a += 1.0
            if a == 0:
                pixels.append((0, 0, 0, 0))
            else:
                total = float(samples * samples)
                pixels.append(
                    (
                        round(r / a),
                        round(g / a),
                        round(b / a),
                        round(255 * a / total),
                    )
                )
    return pixels


def _dib(size: int, pixels: list[tuple[int, int, int, int]]) -> bytes:
    """A 32-bit bottom-up DIB, with the vestigial AND mask an ICO still requires."""
    header = struct.pack(
        "<IiiHHIIiiII",
        40,  # biSize
        size,  # biWidth
        size * 2,  # biHeight: XOR bitmap plus AND mask, per the ICO spec
        1,  # biPlanes
        32,  # biBitCount
        0,  # biCompression = BI_RGB
        0,  # biSizeImage (may be 0 for BI_RGB)
        0,
        0,
        0,
        0,
    )
    body = bytearray()
    for py in reversed(range(size)):  # DIBs are stored bottom-up
        for px in range(size):
            r, g, b, a = pixels[py * size + px]
            body += bytes((b, g, r, a))
    # The AND mask is ignored for 32-bit images but must be present and padded to
    # 4-byte rows. All zeros = "consult the alpha channel".
    body += bytes(((size + 31) // 32) * 4 * size)
    return header + bytes(body)


def build(path: Path) -> None:
    images = []
    for size in SIZES:
        # 4x supersampling matters most where pixels are scarce; at 256 the corners
        # are already smooth and 2x keeps this script under a couple of seconds.
        images.append(_dib(size, render(size, 4 if size <= 48 else 2)))

    offset = 6 + 16 * len(SIZES)
    directory = bytearray(struct.pack("<HHH", 0, 1, len(SIZES)))
    for size, blob in zip(SIZES, images, strict=True):
        directory += struct.pack(
            "<BBBBHHII",
            0 if size == 256 else size,  # 256 is encoded as 0 in a single byte
            0 if size == 256 else size,
            0,  # no palette
            0,
            1,  # planes
            32,  # bits per pixel
            len(blob),
            offset,
        )
        offset += len(blob)

    path.write_bytes(bytes(directory) + b"".join(images))
    print(f"wrote {path.name} ({path.stat().st_size:,} bytes, sizes {', '.join(map(str, SIZES))})")


def main() -> None:
    global MODE
    for MODE, filename in (("app", "ipo-copilot.ico"), ("stop", "ipo-copilot-stop.ico")):
        build(Path(__file__).with_name(filename))


if __name__ == "__main__":
    main()
