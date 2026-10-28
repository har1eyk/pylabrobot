"""Generate masks from recorded pixels and compare independent vendor region fingerprints."""

import base64
import hashlib
import json
import struct
import tempfile
import unittest
import zlib
from dataclasses import replace
from pathlib import Path
from typing import Any

from .acquisition import CameraExposure
from .camera import CameraFrame, CameraGeometry
from .collection import CollectionFilter, CollectionPlan
from .fluorescence import FluorescenceFrame
from .image_processing import PlatePixelMask
from .protocol import ABI7500ProtocolError
from .roi_calibration import ROICalibrationImage, generate_roi_mask

_VECTORS = json.loads(
  (Path(__file__).parent / "test_data/roi_generation_vectors.json").read_text()
)["vectors"]


def _camera(image: bytes) -> CameraFrame:
  """Embed image pixels in a synthetic transfer with deliberately different unused margins."""
  pixels = tuple(reversed(struct.unpack("<76800H", image)))
  wire = [999] * 78645
  for row in range(240):
    start = 321 * (row + 5) - 1
    wire[start : start + 320] = pixels[row * 320 : (row + 1) * 320]
  return CameraFrame(CameraGeometry(321, 245), struct.pack("<78645H", *wire) + b"\xa5" * 22)


def _image(vector: dict[str, Any]) -> ROICalibrationImage:
  """Read only input pixels; expected mask fingerprints never enter the generator."""
  payload = zlib.decompress(base64.b64decode("".join(vector["payload"])))
  if vector["input_kind"] == "camera-transfer":
    return ROICalibrationImage.from_camera(
      CameraFrame(CameraGeometry(321, 245), payload), filter=vector["physical_filter"]
    )
  return ROICalibrationImage(payload, vector["physical_filter"])


def _plate(*, missing: bool = False, extra: bool = False, offset: int = 0) -> ROICalibrationImage:
  """Draw an independently specified grid with one hollow well and optional geometry defects."""
  pixels = [128] * 76800
  for row in range(8):
    for column in range(12):
      if missing and (row, column) == (3, 4):
        continue
      center = (22 + row * 27 + offset, 22 + column * 25 + offset)
      for dr in range(-6, 7):
        for dc in range(-6, 7):
          if (row, column) == (2, 3) and abs(dr) <= 3 and abs(dc) <= 3:
            continue
          pixels[(center[0] + dr) * 320 + center[1] + dc] = 3000
  if extra:
    for row in range(4, 9):
      for column in range(4, 9):
        pixels[row * 320 + column] = 3000
  return ROICalibrationImage(struct.pack("<76800H", *pixels), 1)


class ROICalibrationTests(unittest.TestCase):
  """No USB device, vendor binary or saved AOI input is needed by the implementation."""

  def test_recorded_masks_thresholds_and_measurements(self) -> None:
    """Every selected pixel and adjacent-ring sum matches the independently saved vendor mask."""
    for vector in _VECTORS:
      with self.subTest(reference=vector["reference"], filter=vector["physical_filter"]):
        payload = zlib.decompress(base64.b64decode("".join(vector["payload"])))
        self.assertEqual(hashlib.sha256(payload).hexdigest(), vector["input_sha256"])
        image = _image(vector)
        self.assertEqual(image.sha256, vector["image_sha256"])
        result = generate_roi_mask(image)
        self.assertTrue(result.passed, result.failure_reasons)
        self.assertEqual(result.threshold, vector["vendor_threshold"])
        self.assertEqual(result.mask.source_image_sha256, image.sha256)
        self.assertEqual(result.mask.filter, image.filter)
        measurements = FluorescenceFrame(
          _camera(image.image_bytes), CameraExposure(0.256), result.mask
        )
        for points, well, expected in zip(
          result.mask.wells, measurements.wells, vector["vendor_regions"]
        ):
          encoded = b"".join(struct.pack("<HH", *point) for point in points)
          self.assertEqual(hashlib.sha256(encoded).hexdigest(), expected["coordinate_sha256"])
          self.assertEqual(len(points), expected["pixel_count"])
          self.assertEqual(well.pixel_sum, expected["pixel_sum"])
          self.assertEqual(well.ring_sum, expected["ring_sum"])
          self.assertEqual(well.ring_count, expected["ring_count"])
        report = json.loads(json.dumps(result.report()))
        self.assertEqual(report["mask_sha256"], result.mask.sha256)
        self.assertEqual(report["regions"][0]["well"], "A1")
        self.assertEqual(report["regions"][-1]["well"], "H12")
        self.assertEqual(
          report["saturated_pixels"],
          sum(v == 4095 for v in struct.unpack("<76800H", image.image_bytes)),
        )

  def test_crop_orientation_and_view_independence(self) -> None:
    """An original captured transfer matches the TIFF hash in either camera view mode."""
    vector = _VECTORS[0]
    data = zlib.decompress(base64.b64decode("".join(vector["payload"])))
    source = CameraFrame(CameraGeometry(321, 245), data)
    for view in (False, True):
      image = ROICalibrationImage.from_camera(replace(source, include_optical_black=view), filter=1)
      self.assertEqual(image.sha256, vector["image_sha256"])
    self.assertEqual(source.wire_data, data)
    with self.assertRaisesRegex(ValueError, "321 by 245"):
      ROICalibrationImage.from_camera(CameraFrame(CameraGeometry(2, 6), bytes(128)), filter=1)

  def test_geometry_follows_pixels_and_fills_enclosed_holes(self) -> None:
    """A translated synthetic plate follows its detected regions instead of a fixed well grid."""
    for offset in (0, 2):
      result = generate_roi_mask(_plate(offset=offset))
      self.assertTrue(result.passed, result.failure_reasons)
      for index, region in enumerate(result.regions):
        r, c = 22 + 27 * (index // 12) + offset, 22 + 25 * (index % 12) + offset
        self.assertEqual(
          region, tuple((y, x) for y in range(r - 8, r + 9) for x in range(c - 8, c + 9))
        )

  def test_invalid_geometry_and_contrast_block_export(self) -> None:
    """Missing/extra wells and flat or weak images cannot silently yield a calibration."""
    images = [_plate(missing=True), _plate(extra=True)]
    images.extend(
      ROICalibrationImage(struct.pack("<H", value) * 76800, 1) for value in (0, 128, 4095)
    )
    weak = [value // 32 for value in struct.unpack("<76800H", _plate().image_bytes)]
    images.append(ROICalibrationImage(struct.pack("<76800H", *weak), 1))
    for image in images:
      result = generate_roi_mask(image)
      self.assertFalse(result.passed)
      self.assertTrue(result.failure_reasons)
      self.assertIsNone(result.report()["mask_sha256"])
      with self.assertRaisesRegex(ValueError, "generation failed"):
        result.mask
    passing = generate_roi_mask(_plate())
    for fields in (
      {"regions": passing.regions[:-1]},
      {"regions": passing.regions[::-1]},
      {"threshold_grid": ()},
    ):
      with self.assertRaises(ValueError):
        replace(passing, **fields)

  def test_reject_invalid_image_inputs(self) -> None:
    """Dimensions, pixel range, explicit filter and immutable inputs are checked before work."""
    for data in (b"", bytes(153598), bytearray(153600), b"\xff\xff" * 76800):
      with self.assertRaises(ValueError):
        ROICalibrationImage(data, 1)  # type: ignore[arg-type]
    for filter in (0, 6, True):
      with self.assertRaises(ValueError):
        ROICalibrationImage(bytes(153600), filter)
    with self.assertRaises(TypeError):
      generate_roi_mask(bytes(153600))  # type: ignore[arg-type]

  def test_portable_mask_and_collection_round_trip(self) -> None:
    """Pin generated regions/filter/hash through the existing journal serialization path."""
    result = generate_roi_mask(_image(_VECTORS[2]))
    mask = result.mask
    with tempfile.TemporaryDirectory() as folder:
      path = Path(folder) / "physical-filter-2.plrroi"
      path.write_bytes(mask.source_bytes)
      loaded = PlatePixelMask.from_file(path, filter=2)
    self.assertEqual(loaded, mask)
    self.assertTrue(loaded.generated)
    plan = CollectionPlan(
      ((0, 0),), (CollectionFilter(2, (2.0,), "automatic"),), exposure_search_masks=(loaded,)
    )
    restored = CollectionPlan.from_dict(json.loads(json.dumps(plan.to_dict())))
    self.assertEqual(restored, plan)
    self.assertEqual(restored.exposure_search_masks[0].wells, mask.wells)

  def test_reject_corrupt_or_misassociated_generated_masks(self) -> None:
    """Truncation, trailing bytes, overlap, duplicates and filter changes cannot pass decoding."""
    mask = generate_roi_mask(_plate()).mask
    with self.assertRaises(ABI7500ProtocolError):
      PlatePixelMask(mask.source_bytes, 2)
    for data in (
      mask.source_bytes[:-1],
      mask.source_bytes + b"x",
      b"PLR-ABI7500-ROI\x02" + mask.source_bytes[15:],
    ):
      with self.assertRaises(ABI7500ProtocolError):
        PlatePixelMask(data, 1)
    for regions in (
      mask.wells[:1] + mask.wells[:1] + mask.wells[2:],
      (mask.wells[0][::-1],) + mask.wells[1:],
      ((mask.wells[0][0],) + mask.wells[0],) + mask.wells[1:],
    ):
      with self.assertRaises(ABI7500ProtocolError):
        PlatePixelMask.from_regions(regions, filter=1, source_image_sha256=mask.source_image_sha256)  # type: ignore[arg-type]
    with self.assertRaises(ValueError):
      PlatePixelMask.from_regions(mask.wells, filter=1, source_image_sha256="not-a-hash")


if __name__ == "__main__":
  unittest.main()
