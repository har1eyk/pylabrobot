"""Read the captured SDS2 background section without loading vendor code."""

import math
import struct


def _chunks(data: bytes, *, counted: bool) -> tuple[tuple[bytes, bytes], ...]:
  """Decode bounded big-endian tag/length records, optionally preceded by a child count."""
  if counted and len(data) < 2:
    raise ValueError("Truncated SDA child count")
  expected = int.from_bytes(data[:2], "big") if counted else None
  position = 2 if counted else 0
  result = []
  while position < len(data):
    if len(data) - position < 8:
      raise ValueError("Truncated SDA record header")
    tag = data[position : position + 4]
    size = int.from_bytes(data[position + 4 : position + 8], "big")
    position += 8
    if size > len(data) - position:
      raise ValueError("SDA record extends beyond its parent")
    result.append((tag, data[position : position + size]))
    position += size
  if expected is not None and len(result) != expected:
    raise ValueError("SDA child count disagrees with its records")
  return tuple(result)


def _fields(data: bytes, tags: set[bytes]) -> dict[bytes, bytes]:
  """Require exactly one instance of each expected child tag."""
  children = _chunks(data, counted=True)
  result = dict(children)
  if len(result) != len(children) or result.keys() != tags:
    raise ValueError("Unexpected or duplicate SDA fields")
  return result


def read_background_sda(data: bytes) -> tuple[tuple[float, ...], ...]:
  """Read full-precision offsets/slopes for the captured 96-well I809 profile.

  The SDS2 header and structural lengths are validated; unrelated top-level sections are
  skipped by their lengths. This does not certify the file's instrument association, age,
  checksum or calibration validity. No coefficients are rounded through a text export.
  """
  if len(data) < 1024 or data[:8] != b"SDS2\x00\x02\x00\x04":
    raise ValueError("Unsupported calibration SDA header")
  if data[16:24] != b"p9C \x00\x08\x00\x0c":
    raise ValueError("Expected the captured 96-well SDA geometry")
  top = _chunks(data[1024:], counted=False)
  sections = [value for tag, value in top if tag == b"CLBl"]
  if len(sections) != 1:
    raise ValueError("SDA requires exactly one background section")
  backgrounds = _fields(sections[0], {b"CLBg"})
  background = _fields(backgrounds[b"CLBg"], {b"CLPf", b"CalP"})
  profile = _fields(background[b"CLPf"], {b"InId", b"CPRM", b"XTak"})
  if profile != {b"InId": b"\x00\x00\x03\x29", b"CPRM": b"\x00", b"XTak": b"\x01"}:
    raise ValueError("Unsupported SDA background profile or extraction settings")
  wells = _chunks(background[b"CalP"], counted=True)
  if len(wells) != 96 or any(tag != b"CaWe" for tag, _ in wells):
    raise ValueError("SDA background requires 96 well records")
  values: dict[int, tuple[float, ...]] = {}
  for _, record in wells:
    fields = _fields(record, {b"WelN", b"WCFA", b"TSMP"})
    if len(fields[b"WelN"]) != 2 or len(fields[b"WCFA"]) != 40 or len(fields[b"TSMP"]) != 4:
      raise ValueError("Invalid SDA background well field length")
    well = int.from_bytes(fields[b"WelN"], "big")
    if well >= 96 or well in values:
      raise ValueError("Duplicate or out-of-range SDA well index")
    coefficients = struct.unpack("<10f", fields[b"WCFA"])
    if not all(math.isfinite(value) for value in coefficients):
      raise ValueError("SDA background coefficients must be finite")
    values[well] = coefficients
  # WCFA stores offset/slope pairs in physical filter order; callers use saved-filter order.
  return tuple(
    tuple(values[well][2 * physical + kind] for well in range(96))
    for kind in (0, 1)
    for physical in reversed(range(5))
  )
