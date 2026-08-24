"""Structural envelope inspection for byte-exact PonyChart image responses.

This module deliberately avoids a heavyweight pixel codec in the browser and
retention processes.  It verifies container structure and encoded dimensions;
the browser's loaded-image receipt and the classifier perform actual decoding.
"""

from __future__ import annotations

import binascii
import struct
import zlib
from dataclasses import dataclass

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_START = b"\xff\xd8"
_JPEG_END = b"\xff\xd9"
_MAX_DECOMPRESSED_IMAGE_BYTES = 128 * 1024 * 1024
_JPEG_START_OF_FRAME_MARKERS = frozenset(
    {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
)


@dataclass(frozen=True, slots=True)
class PonyChartImageInfo:
    """Structurally inspected properties derived from the response bytes."""

    media_type: str
    extension: str
    width: int
    height: int


def _validate_png(image: bytes) -> tuple[int, int]:
    if not image.startswith(_PNG_SIGNATURE):
        raise ValueError("PonyChart response was not a PNG image")

    offset = len(_PNG_SIGNATURE)
    dimensions: tuple[int, int] | None = None
    bits_per_pixel: int | None = None
    interlace_method: int | None = None
    png_bit_depth: int | None = None
    png_color_type: int | None = None
    compressed = bytearray()
    saw_image_data = False
    ended_image_data = False
    saw_palette = False
    while True:
        if offset + 12 > len(image):
            raise ValueError("PonyChart PNG contained a truncated chunk")

        chunk_length = struct.unpack_from(">I", image, offset)[0]
        chunk_type = image[offset + 4 : offset + 8]
        data_start = offset + 8
        data_end = data_start + chunk_length
        chunk_end = data_end + 4
        if chunk_end > len(image):
            raise ValueError("PonyChart PNG contained a truncated chunk")

        chunk_data = image[data_start:data_end]
        expected_crc = struct.unpack_from(">I", image, data_end)[0]
        actual_crc = binascii.crc32(chunk_type + chunk_data) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            raise ValueError("PonyChart PNG contained an invalid checksum")

        if dimensions is None:
            if chunk_type != b"IHDR" or chunk_length != 13:
                raise ValueError("PonyChart PNG did not begin with IHDR")
            width, height, bit_depth, color_type, compression, filtering, interlace = (
                struct.unpack(">IIBBBBB", chunk_data)
            )
            if width <= 0 or height <= 0:
                raise ValueError("PonyChart PNG dimensions must be positive")
            valid_depths = {
                0: frozenset({1, 2, 4, 8, 16}),
                2: frozenset({8, 16}),
                3: frozenset({1, 2, 4, 8}),
                4: frozenset({8, 16}),
                6: frozenset({8, 16}),
            }
            channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
            if bit_depth not in valid_depths.get(color_type, frozenset()):
                raise ValueError("PonyChart PNG used an invalid color depth")
            if compression != 0 or filtering != 0 or interlace not in {0, 1}:
                raise ValueError("PonyChart PNG used unsupported coding metadata")
            dimensions = (width, height)
            bits_per_pixel = channels[color_type] * bit_depth
            interlace_method = interlace
            png_bit_depth = bit_depth
            png_color_type = color_type
        elif chunk_type == b"IHDR":
            raise ValueError("PonyChart PNG contained more than one IHDR")

        if chunk_type == b"PLTE":
            assert png_bit_depth is not None
            assert png_color_type is not None
            palette_entries = chunk_length // 3
            if (
                saw_image_data
                or saw_palette
                or not 0 < chunk_length <= 768
                or chunk_length % 3 != 0
                or png_color_type in {0, 4}
                or (png_color_type == 3 and palette_entries > 2**png_bit_depth)
            ):
                raise ValueError("PonyChart PNG contained an invalid palette")
            saw_palette = True
        elif chunk_type == b"IDAT":
            if ended_image_data:
                raise ValueError("PonyChart PNG image data was not contiguous")
            saw_image_data = True
            compressed.extend(chunk_data)
        elif chunk_type == b"IEND":
            if chunk_length != 0 or not saw_image_data:
                raise ValueError("PonyChart PNG was incomplete")
            if chunk_end != len(image):
                raise ValueError("PonyChart PNG had data after IEND")
            assert dimensions is not None
            assert bits_per_pixel is not None
            assert interlace_method is not None
            if png_color_type == 3 and not saw_palette:
                raise ValueError("PonyChart indexed PNG lacked a palette")
            expected_scanlines = _png_expected_scanlines(
                dimensions[0],
                dimensions[1],
                bits_per_pixel=bits_per_pixel,
                interlace_method=interlace_method,
            )
            _validate_png_scanlines(bytes(compressed), expected_scanlines)
            return dimensions
        elif saw_image_data:
            ended_image_data = True

        if (
            chunk_type not in {b"IHDR", b"PLTE", b"IDAT", b"IEND"}
            and chunk_type[:1].isupper()
        ):
            raise ValueError("PonyChart PNG contained an unknown critical chunk")

        offset = chunk_end


def _png_expected_scanlines(
    width: int,
    height: int,
    *,
    bits_per_pixel: int,
    interlace_method: int,
) -> tuple[tuple[int, int], ...]:
    passes = (
        ((0, 0, 1, 1),)
        if interlace_method == 0
        else (
            (0, 0, 8, 8),
            (4, 0, 8, 8),
            (0, 4, 4, 8),
            (2, 0, 4, 4),
            (0, 2, 2, 4),
            (1, 0, 2, 2),
            (0, 1, 1, 2),
        )
    )
    scanline_groups: list[tuple[int, int]] = []
    decoded_size = 0
    for x_start, y_start, x_step, y_step in passes:
        pass_width = max(0, (width - x_start + x_step - 1) // x_step)
        pass_height = max(0, (height - y_start + y_step - 1) // y_step)
        if pass_width == 0 or pass_height == 0:
            continue
        row_bytes = (pass_width * bits_per_pixel + 7) // 8
        decoded_size += (row_bytes + 1) * pass_height
        if decoded_size > _MAX_DECOMPRESSED_IMAGE_BYTES:
            raise ValueError("PonyChart PNG decompressed image exceeded its size limit")
        scanline_groups.append((row_bytes, pass_height))
    return tuple(scanline_groups)


def _validate_png_scanlines(
    compressed: bytes,
    scanline_groups: tuple[tuple[int, int], ...],
) -> None:
    expected_size = sum(
        (row_bytes + 1) * row_count for row_bytes, row_count in scanline_groups
    )
    decompressor = zlib.decompressobj()
    try:
        decoded = decompressor.decompress(compressed, expected_size + 1)
        if len(decoded) <= expected_size:
            decoded += decompressor.flush(expected_size + 1 - len(decoded))
    except zlib.error as error:
        raise ValueError("PonyChart PNG contained invalid zlib image data") from error
    if (
        len(decoded) != expected_size
        or not decompressor.eof
        or decompressor.unused_data
        or decompressor.unconsumed_tail
    ):
        raise ValueError("PonyChart PNG image data did not match its dimensions")
    offset = 0
    for row_bytes, row_count in scanline_groups:
        row_size = row_bytes + 1
        group_end = offset + row_size * row_count
        if any(decoded[index] > 4 for index in range(offset, group_end, row_size)):
            raise ValueError("PonyChart PNG contained an invalid scanline filter")
        offset = group_end


def _validate_jpeg(image: bytes) -> tuple[int, int]:
    if not image.startswith(_JPEG_START) or not image.endswith(_JPEG_END):
        raise ValueError("PonyChart response was not a complete JPEG image")

    offset = len(_JPEG_START)
    dimensions: tuple[int, int] | None = None
    saw_scan = False
    pending_marker: int | None = None
    while offset < len(image) or pending_marker is not None:
        if pending_marker is None:
            if image[offset] != 0xFF:
                raise ValueError("PonyChart JPEG contained an invalid marker")
            while offset < len(image) and image[offset] == 0xFF:
                offset += 1
            if offset >= len(image):
                break
            marker = image[offset]
            offset += 1
        else:
            marker = pending_marker
            pending_marker = None

        if marker in {0x00, 0xD8} or 0xD0 <= marker <= 0xD7:
            raise ValueError("PonyChart JPEG contained an invalid marker sequence")
        if marker == 0xD9:
            if offset != len(image) or dimensions is None or not saw_scan:
                raise ValueError("PonyChart JPEG ended before a complete scan")
            return dimensions
        if marker == 0x01:
            continue
        if offset + 2 > len(image):
            raise ValueError("PonyChart JPEG contained a truncated segment")
        segment_length = struct.unpack_from(">H", image, offset)[0]
        segment_end = offset + segment_length
        if segment_length < 2 or segment_end > len(image):
            raise ValueError("PonyChart JPEG contained an invalid segment length")

        if marker in _JPEG_START_OF_FRAME_MARKERS:
            if segment_length < 8:
                raise ValueError("PonyChart JPEG contained a truncated SOF segment")
            height, width = struct.unpack_from(">HH", image, offset + 3)
            component_count = image[offset + 7]
            if (
                width <= 0
                or height <= 0
                or component_count == 0
                or segment_length != 8 + 3 * component_count
            ):
                raise ValueError("PonyChart JPEG contained an invalid SOF segment")
            if dimensions is not None and dimensions != (width, height):
                raise ValueError("PonyChart JPEG contained conflicting dimensions")
            dimensions = (width, height)

        if marker != 0xDA:
            offset = segment_end
            continue
        if dimensions is None or segment_length < 8:
            raise ValueError("PonyChart JPEG reached SOS before valid dimensions")
        scan_components = image[offset + 2]
        if scan_components == 0 or segment_length != 6 + 2 * scan_components:
            raise ValueError("PonyChart JPEG contained an invalid SOS segment")
        saw_scan = True
        offset = segment_end
        entropy_bytes = 0
        while offset < len(image):
            value = image[offset]
            offset += 1
            if value != 0xFF:
                entropy_bytes += 1
                continue
            while offset < len(image) and image[offset] == 0xFF:
                offset += 1
            if offset >= len(image):
                break
            entropy_marker = image[offset]
            offset += 1
            if entropy_marker == 0x00:
                entropy_bytes += 1
                continue
            if 0xD0 <= entropy_marker <= 0xD7:
                continue
            if entropy_bytes == 0:
                raise ValueError("PonyChart JPEG scan contained no entropy data")
            pending_marker = entropy_marker
            break

    raise ValueError("PonyChart JPEG did not contain a complete image scan")


def _little_uint24(value: bytes) -> int:
    if len(value) != 3:
        raise ValueError("WebP uint24 requires exactly three bytes")
    return value[0] | value[1] << 8 | value[2] << 16


def _webp_bitstream_dimensions(chunk_type: bytes, payload: bytes) -> tuple[int, int]:
    if chunk_type == b"VP8L":
        if len(payload) <= 5 or payload[0] != 0x2F:
            raise ValueError("PonyChart WebP contained an invalid VP8L bitstream")
        width = 1 + payload[1] + ((payload[2] & 0x3F) << 8)
        height = 1 + (payload[2] >> 6) + (payload[3] << 2) + ((payload[4] & 0x0F) << 10)
        if payload[4] & 0xE0:
            raise ValueError("PonyChart WebP VP8L header used reserved bits")
        return width, height
    if chunk_type == b"VP8 ":
        if len(payload) <= 10 or payload[3:6] != b"\x9d\x01\x2a":
            raise ValueError("PonyChart WebP contained an invalid VP8 bitstream")
        width = struct.unpack_from("<H", payload, 6)[0] & 0x3FFF
        height = struct.unpack_from("<H", payload, 8)[0] & 0x3FFF
        if width <= 0 or height <= 0:
            raise ValueError("PonyChart WebP dimensions must be positive")
        return width, height
    raise ValueError("PonyChart WebP did not contain an image bitstream")


def _webp_animation_frame_dimensions(payload: bytes) -> tuple[int, int]:
    if len(payload) < 24:
        raise ValueError("PonyChart WebP contained a truncated animation frame")
    if payload[15] & 0xFC:
        raise ValueError("PonyChart WebP animation frame used reserved flags")
    width = _little_uint24(payload[6:9]) + 1
    height = _little_uint24(payload[9:12]) + 1
    offset = 16
    bitstream_dimensions: tuple[int, int] | None = None
    while offset + 8 <= len(payload):
        chunk_type = payload[offset : offset + 4]
        chunk_size = struct.unpack_from("<I", payload, offset + 4)[0]
        data_start = offset + 8
        data_end = data_start + chunk_size
        padded_end = data_end + (chunk_size & 1)
        if data_end > len(payload) or padded_end > len(payload):
            raise ValueError("PonyChart WebP animation frame was truncated")
        if chunk_type in {b"VP8 ", b"VP8L"}:
            if bitstream_dimensions is not None:
                raise ValueError("PonyChart WebP frame had multiple bitstreams")
            bitstream_dimensions = _webp_bitstream_dimensions(
                chunk_type,
                payload[data_start:data_end],
            )
        offset = padded_end
    if offset != len(payload) or bitstream_dimensions is None:
        raise ValueError("PonyChart WebP frame lacked a complete bitstream")
    if bitstream_dimensions != (width, height):
        raise ValueError("PonyChart WebP frame dimensions conflicted")
    return width, height


def _validate_webp(image: bytes) -> tuple[int, int]:
    if len(image) < 20 or image[:4] != b"RIFF" or image[8:12] != b"WEBP":
        raise ValueError("PonyChart response was not a WebP image")
    riff_size = struct.unpack_from("<I", image, 4)[0]
    if riff_size + 8 != len(image):
        raise ValueError("PonyChart WebP RIFF size did not match its body")

    offset = 12
    canvas_dimensions: tuple[int, int] | None = None
    bitstream_dimensions: tuple[int, int] | None = None
    animation = False
    saw_animation_header = False
    saw_animation_frame = False
    while offset + 8 <= len(image):
        chunk_type = image[offset : offset + 4]
        chunk_size = struct.unpack_from("<I", image, offset + 4)[0]
        data_start = offset + 8
        data_end = data_start + chunk_size
        padded_end = data_end + (chunk_size & 1)
        if data_end > len(image) or padded_end > len(image):
            raise ValueError("PonyChart WebP contained a truncated chunk")
        payload = image[data_start:data_end]

        if chunk_type == b"VP8X":
            if offset != 12 or canvas_dimensions is not None or len(payload) != 10:
                raise ValueError("PonyChart WebP contained an invalid VP8X chunk")
            if payload[1:4] != b"\x00\x00\x00" or payload[0] & 0xC1:
                raise ValueError("PonyChart WebP VP8X header used reserved bits")
            width = _little_uint24(payload[4:7]) + 1
            height = _little_uint24(payload[7:10]) + 1
            canvas_dimensions = (width, height)
            animation = bool(payload[0] & 0x02)
        elif chunk_type in {b"VP8 ", b"VP8L"}:
            if bitstream_dimensions is not None:
                raise ValueError("PonyChart WebP contained multiple bitstreams")
            bitstream_dimensions = _webp_bitstream_dimensions(chunk_type, payload)
        elif chunk_type == b"ANIM":
            if len(payload) != 6 or saw_animation_header:
                raise ValueError("PonyChart WebP contained an invalid ANIM chunk")
            saw_animation_header = True
        elif chunk_type == b"ANMF":
            if not saw_animation_header:
                raise ValueError("PonyChart WebP frame preceded its animation header")
            frame_width, frame_height = _webp_animation_frame_dimensions(payload)
            if canvas_dimensions is None:
                raise ValueError("PonyChart WebP animation lacked a canvas")
            x = 2 * _little_uint24(payload[0:3])
            y = 2 * _little_uint24(payload[3:6])
            if (
                x + frame_width > canvas_dimensions[0]
                or y + frame_height > canvas_dimensions[1]
            ):
                raise ValueError("PonyChart WebP animation frame exceeded its canvas")
            saw_animation_frame = True

        offset = padded_end

    if offset != len(image):
        raise ValueError("PonyChart WebP ended with a truncated chunk")
    if animation:
        if bitstream_dimensions is not None or not (
            saw_animation_header and saw_animation_frame
        ):
            raise ValueError("PonyChart WebP animation was incomplete")
        assert canvas_dimensions is not None
        return canvas_dimensions
    if saw_animation_header or saw_animation_frame or bitstream_dimensions is None:
        raise ValueError("PonyChart WebP lacked a complete image bitstream")
    if canvas_dimensions is not None and canvas_dimensions != bitstream_dimensions:
        raise ValueError("PonyChart WebP canvas dimensions conflicted")
    return canvas_dimensions or bitstream_dimensions


def inspect_ponychart_image(image: bytes) -> PonyChartImageInfo:
    """Inspect a supported container envelope, dimensions, and native suffix.

    JPEG/WebP entropy is not pixel-decoded here.  Callers acquiring a live
    challenge additionally require the browser's successful natural-dimension
    receipt for these exact bytes before accepting them.
    """

    if image.startswith(_PNG_SIGNATURE):
        width, height = _validate_png(image)
        return PonyChartImageInfo("image/png", ".png", width, height)
    if image.startswith(_JPEG_START):
        width, height = _validate_jpeg(image)
        return PonyChartImageInfo("image/jpeg", ".jpg", width, height)
    if image.startswith(b"RIFF") and image[8:12] == b"WEBP":
        width, height = _validate_webp(image)
        return PonyChartImageInfo("image/webp", ".webp", width, height)
    raise ValueError("PonyChart response used an unsupported image format")
