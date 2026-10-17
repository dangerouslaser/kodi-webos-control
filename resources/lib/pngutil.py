"""Just enough PNG to re-lay-out a cursor image (Kodi has no image library).

Reads 8-bit RGBA or RGB, non-interlaced PNGs and writes 8-bit RGBA.
"""

from __future__ import annotations

import struct
import zlib

SIGNATURE = b"\x89PNG\r\n\x1a\n"


class PngError(ValueError):
    pass


def read_rgba(data: bytes) -> tuple[int, int, bytearray]:
    """(width, height, RGBA pixels row by row)."""
    if not data.startswith(SIGNATURE):
        raise PngError("not a PNG")
    pos, idat, header = len(SIGNATURE), [], None
    while pos + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
    if header is None:
        raise PngError("no IHDR")
    width, height, depth, color, _comp, _filter, interlace = header
    if depth != 8 or color not in (2, 6) or interlace:
        raise PngError(f"unsupported PNG (depth {depth}, color type {color}, interlace {interlace})")
    channels = 4 if color == 6 else 3
    raw = zlib.decompress(b"".join(idat))
    stride = width * channels
    out = bytearray(width * height * 4)
    prev = bytearray(stride)
    for y in range(height):
        start = y * (stride + 1)
        kind = raw[start]
        line = bytearray(raw[start + 1:start + 1 + stride])
        _unfilter(kind, line, prev, channels)
        for x in range(width):
            o = (y * width + x) * 4
            i = x * channels
            out[o:o + 3] = line[i:i + 3]
            out[o + 3] = line[i + 3] if channels == 4 else 255
        prev = line
    return width, height, out


def _unfilter(kind: int, line: bytearray, prev: bytearray, bpp: int) -> None:
    for i in range(len(line)):
        a = line[i - bpp] if i >= bpp else 0
        b = prev[i]
        c = prev[i - bpp] if i >= bpp else 0
        if kind == 1:
            line[i] = (line[i] + a) & 0xFF
        elif kind == 2:
            line[i] = (line[i] + b) & 0xFF
        elif kind == 3:
            line[i] = (line[i] + (a + b) // 2) & 0xFF
        elif kind == 4:
            p = a + b - c
            pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
            pred = a if pa <= pb and pa <= pc else b if pb <= pc else c
            line[i] = (line[i] + pred) & 0xFF
        elif kind != 0:
            raise PngError(f"bad filter {kind}")


def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def _deflated(width: int, height: int, pixels: bytes | bytearray) -> bytes:
    stride = width * 4
    return zlib.compress(b"".join(b"\0" + bytes(pixels[y * stride:(y + 1) * stride]) for y in range(height)), 9)


def _header(width: int, height: int) -> bytes:
    return SIGNATURE + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))


def write_rgba(width: int, height: int, pixels: bytes | bytearray) -> bytes:
    return _header(width, height) + _chunk(b"IDAT", _deflated(width, height, pixels)) + _chunk(b"IEND", b"")


def write_apng(width: int, height: int, frames: list[bytes | bytearray], delays: list[int]) -> bytes:
    """An animated PNG (full-size RGBA frames, each shown for its delay in ms,
    at most 65535). It loops; Kodi plays every animation in a loop, so one
    meant to play once ends on a long last delay."""
    out = [_header(width, height), _chunk(b"acTL", struct.pack(">II", len(frames), 0))]
    seq = 0
    for i, (pixels, delay) in enumerate(zip(frames, delays)):
        # fcTL: size, offset, delay (numerator/denominator: ms), dispose none, blend source.
        out.append(_chunk(b"fcTL", struct.pack(">IIIIIHHBB", seq, width, height, 0, 0,
                                               min(int(delay), 65535), 1000, 0, 0)))
        seq += 1
        data = _deflated(width, height, pixels)
        if i == 0:
            out.append(_chunk(b"IDAT", data))
        else:
            out.append(_chunk(b"fdAT", struct.pack(">I", seq) + data))
            seq += 1
    out.append(_chunk(b"IEND", b""))
    return b"".join(out)


def tip(width: int, height: int, pixels: bytearray, threshold: int = 128) -> tuple[int, int]:
    """The pointer's hotspot: the middle of its topmost solid row (a fingertip, a drop's tip)."""
    for y in range(height):
        xs = [x for x in range(width) if pixels[(y * width + x) * 4 + 3] >= threshold]
        if xs:
            return (xs[0] + xs[-1]) // 2, y
    return 0, 0


def corner(width: int, height: int, pixels: bytearray, threshold: int = 128,
           which: str = "tl") -> tuple[int, int]:
    """The pointer's hotspot when it's the shape's pointed corner: top-left
    ("tl"), top-right ("tr"), bottom-left ("bl") or bottom-right ("br")."""
    right, bottom = "r" in which, which.startswith("b")
    best: tuple[int, int, int] | None = None
    for y in range(height):
        for x in range(width):
            if pixels[(y * width + x) * 4 + 3] < threshold:
                continue
            score = (width - 1 - x if right else x) + (height - 1 - y if bottom else y)
            if best is None or score < best[0]:
                best = (score, x, y)
    return (best[1], best[2]) if best else (0, 0)


def place(width: int, height: int, pixels: bytearray, hotspot: tuple[int, int],
          canvas: tuple[int, int], at: tuple[int, int]) -> bytearray:
    """Copy the image onto a transparent canvas so its hotspot lands on `at` (clipped)."""
    cw, ch = canvas
    out = bytearray(cw * ch * 4)
    dx, dy = at[0] - hotspot[0], at[1] - hotspot[1]
    for y in range(height):
        ty = y + dy
        if not 0 <= ty < ch:
            continue
        x0, x1 = max(0, -dx), min(width, cw - dx)
        if x0 >= x1:
            continue
        src = (y * width + x0) * 4
        dst = (ty * cw + x0 + dx) * 4
        out[dst:dst + (x1 - x0) * 4] = pixels[src:src + (x1 - x0) * 4]
    return out
