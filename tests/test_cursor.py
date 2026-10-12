"""The TV's own pointer images: PNG handling and the fixed Pointer.xml layout."""

from __future__ import annotations

import struct
import zlib

import pytest

from resources.lib import cursor, pngutil


def image(width, height, solid):
    """RGBA pixels: opaque red where solid(x, y), transparent elsewhere."""
    px = bytearray(width * height * 4)
    for y in range(height):
        for x in range(width):
            if solid(x, y):
                px[(y * width + x) * 4:(y * width + x) * 4 + 4] = b"\xff\x00\x00\xff"
    return px


def encode(width, height, px, filters):
    """A PNG using the given filter type per row (cycling), like real encoders do."""
    stride, rows, prev = width * 4, [], bytearray(width * 4)
    for y in range(height):
        line = px[y * stride:(y + 1) * stride]
        kind = filters[y % len(filters)]
        out = bytearray(stride)
        for i in range(stride):
            a = line[i - 4] if i >= 4 else 0
            b, c = prev[i], (prev[i - 4] if i >= 4 else 0)
            if kind == 0:
                pred = 0
            elif kind == 1:
                pred = a
            elif kind == 2:
                pred = b
            elif kind == 3:
                pred = (a + b) // 2
            else:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if pa <= pb and pa <= pc else b if pb <= pc else c
            out[i] = (line[i] - pred) & 0xFF
        rows.append(bytes([kind]) + bytes(out))
        prev = line

    def chunk(k, body):
        return struct.pack(">I", len(body)) + k + body + struct.pack(">I", zlib.crc32(k + body))

    return (pngutil.SIGNATURE + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))


# A hand-ish pointer: a finger from (10, 3) down, a palm below.
def finger(x, y):
    return (9 <= x <= 11 and y >= 3) or (5 <= x <= 20 and y >= 12)


@pytest.mark.parametrize("filters", [[0], [1], [2], [3], [4], [0, 1, 2, 3, 4]])
def test_png_round_trip_with_every_filter(filters):
    px = image(24, 20, finger)
    w, h, decoded = pngutil.read_rgba(encode(24, 20, px, filters))
    assert (w, h) == (24, 20)
    assert decoded == px
    assert pngutil.read_rgba(pngutil.write_rgba(24, 20, px))[2] == px


def test_tip_is_the_top_of_the_finger():
    assert pngutil.tip(24, 20, image(24, 20, finger)) == (10, 3)


def test_rejects_what_it_cant_read():
    with pytest.raises(pngutil.PngError):
        pngutil.read_rgba(b"not a png")


def test_install_lays_pointers_out_for_the_skin(tmp_path):
    png = encode(24, 20, image(24, 20, finger), [0, 1, 2, 3, 4])
    (tmp_path / "point-old.png").write_bytes(b"stale")
    paths = cursor.install({"point": png, "hover": png, "drag": b"broken"}, str(tmp_path))
    assert set(paths) == {"point", "hover"}  # unreadable images are skipped
    assert not (tmp_path / "point-old.png").exists()
    w, h, px = pngutil.read_rgba(open(paths["point"], "rb").read())
    assert (w, h) == cursor.CANVAS
    assert pngutil.tip(w, h, px) == cursor.HOTSPOT  # fingertip on the skin's hotspot
    # Same image again: same file name (Kodi caches textures by path).
    assert cursor.install({"point": png}, str(tmp_path))["point"] == paths["point"]
