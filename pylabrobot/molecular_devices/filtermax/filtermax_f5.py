"""Directly validated Molecular Devices FilterMax F5 support."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from typing import Dict, List, Optional, Sequence, Tuple

from pylabrobot.io.serial import Serial

from .catalog import FilterSlideCatalog
from .errors import (
  FilterMaxCatalogError,
  FilterMaxDeviceError,
  FilterMaxIdentityError,
  FilterMaxProtocolError,
  FilterMaxReadCancelled,
  FilterMaxSlideNotInstalledError,
  FilterMaxUnsupportedOperationError,
)
from .models import (
  AbsorbanceResult,
  FilterKind,
  FilterMaxStatus,
  FluorescencePolarizationResult,
  FluorescenceResult,
  InstalledFilterSlides,
  InstrumentInfo,
  KineticTiming,
  LuminescenceResult,
  PlateGeometry,
  ShakePattern,
  ShakeSpeed,
  ShakingSettings,
  TimeResolvedFluorescenceResult,
  WellScanPoint,
  WellScanSettings,
  empty_plate_data,
)
from .protocol import FilterMaxMessage, FilterMaxTransport, _CancelRequested

logger = logging.getLogger(__name__)

_F5_DEVICE_CODE = 57855
_ERROR_RE = re.compile(r"^- E(?P<code>\d+):\s*(?P<detail>.*)$", re.DOTALL)
_WELL_RE = re.compile(r"^(?P<row>[A-Z]+)(?P<column>[1-9]\d*)$")
_SHAKE_WIRE: Dict[ShakePattern, Tuple[str, int]] = {
  "linear": ("X", 0),
  "orbital": ("O", 2),
}
_SHAKE_SPEED_WIRE: Dict[ShakeSpeed, int] = {"low": 50, "medium": 40, "high": 25}


def _hundredths(value: float) -> int:
  return int(round(value * 100))


class FilterMaxF5:
  """Current v1 driver for the Molecular Devices FilterMax F5.

  The serial protocol is not the SpectraMax ``!COMMAND`` protocol. It uses the captured
  38,400-baud, 7E1 ASTM-like framing implemented privately by :class:`FilterMaxTransport`.
  The full-plate 450 nm absorbance path has been directly validated against an F5 and produced
  results consistent with the corresponding SoftMax Pro workflow.
  """

  def __init__(
    self,
    name: str,
    port: str,
    filter_slide_catalog: Optional[FilterSlideCatalog] = None,
  ) -> None:
    self.name = name
    self.port = port
    self.filter_slide_catalog = filter_slide_catalog
    self.io = Serial(
      human_readable_device_name=f"Molecular Devices FilterMax F5 {name}",
      port=port,
      baudrate=38400,
      bytesize=7,
      parity="E",
      stopbits=1,
      timeout=0.2,
      write_timeout=1,
      rtscts=False,
      dsrdtr=False,
      xonxoff=False,
    )
    self._transport = FilterMaxTransport(self.io)
    self._operation_lock = asyncio.Lock()
    self._cancel_event = asyncio.Event()
    self._read_done = asyncio.Event()
    self._read_done.set()
    self._read_active = False
    self._setup_complete = False
    self._instrument_info: Optional[InstrumentInfo] = None
    self._errors: List[FilterMaxDeviceError] = []

  async def setup(self) -> None:
    await self.io.setup()
    try:
      info = await self.get_instrument_info()
      if info.device_code != _F5_DEVICE_CODE or info.model != "Anthos Fluoro":
        raise FilterMaxIdentityError(
          f"Expected FilterMax F5 device code {_F5_DEVICE_CODE}, received "
          f"{info.model!r} / {info.device_code}"
        )
      self._instrument_info = info
      self._setup_complete = True
      logger.info("[%s] connected to FilterMax F5 serial %s", self.name, info.serial_number)
    except BaseException:
      await self.io.stop()
      raise

  async def stop(self) -> None:
    if self._read_active:
      await self.cancel_read()
    await self.io.stop()
    self._setup_complete = False
    logger.info("[%s] disconnected", self.name)

  async def _command(self, payload: str, timeout: float = 60.0) -> str:
    try:
      return (await self._transport.exchange(payload, timeout)).payload
    except FilterMaxDeviceError as error:
      self._errors.append(error)
      logger.error("[%s] FilterMax command failed: %s", self.name, error)
      raise

  @staticmethod
  def _parse_instrument_info(payload: str) -> InstrumentInfo:
    lines = payload.lstrip("+\r").split("\r")
    if len(lines) < 12:
      raise FilterMaxProtocolError(f"Malformed FilterMax identity response: {payload!r}")
    values: Dict[str, str] = {}
    for line in lines[4:]:
      if ":" in line:
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip()
    try:
      return InstrumentInfo(
        model=lines[0],
        firmware=lines[1],
        device_number=int(lines[2]),
        serial_number=lines[3],
        excitation_slide_id=int(values["Excitation Filter Slider Nr"]),
        emission_slide_id=int(values["Emission Filter Slider Nr"]),
        device_code=int(values["Device Code"]),
        plate_control=values["Plate Control"].upper() == "TRUE",
        pic_firmware=values["PIC Firmware"].lstrip("+ "),
        cpld_version=int(values["CPLD Version"]),
        raw=payload,
      )
    except (KeyError, ValueError) as exc:
      raise FilterMaxProtocolError(f"Malformed FilterMax identity fields: {payload!r}") from exc

  async def get_instrument_info(self) -> InstrumentInfo:
    return self._parse_instrument_info(await self._command("?", timeout=10))

  async def get_status(self) -> FilterMaxStatus:
    text = (await self._command("STAT", timeout=5)).lstrip("+ ")
    state_payload = (await self._command("SGS", timeout=5)).lstrip("+ ")
    try:
      state_bits = tuple(int(value) for value in state_payload.split())
    except ValueError as exc:
      raise FilterMaxProtocolError(f"Malformed SGS response: {state_payload!r}") from exc
    return FilterMaxStatus(ready=text == "READY", text=text, state_bits=state_bits)

  async def get_error_log(self) -> Tuple[FilterMaxDeviceError, ...]:
    """Return errors observed by this driver session.

    SoftMax did not issue a separate persistent-error-log query in the captured F5 protocol.
    Consequently this method intentionally reports the exact device errors received by PLR.
    """

    return tuple(self._errors)

  async def clear_error_log(self) -> None:
    self._errors.clear()

  async def move_plate_tray_out(self) -> None:
    await self._command("E P", timeout=30)

  async def move_plate_tray_in(self) -> None:
    await self._command("L P", timeout=30)

  async def get_filter_slides(self) -> InstalledFilterSlides:
    payload = (await self._command("CS", timeout=5)).lstrip("+ ")
    try:
      excitation, emission = (int(value) for value in payload.split())
    except (ValueError, TypeError) as exc:
      raise FilterMaxProtocolError(f"Malformed CS response: {payload!r}") from exc
    return InstalledFilterSlides(excitation_slide_id=excitation, emission_slide_id=emission)

  async def move_filter_slide_out(self, kind: FilterKind) -> None:
    await self._command(f"E {'A' if kind == 'excitation' else 'B'}", timeout=30)

  async def move_filter_slide_in(self, kind: FilterKind) -> None:
    await self._command(f"L {'A' if kind == 'excitation' else 'B'}", timeout=30)

  async def get_temperature(self) -> float:
    payload = (await self._command("TG", timeout=5)).lstrip("+ ")
    try:
      return float(payload)
    except ValueError as exc:
      raise FilterMaxProtocolError(f"Malformed TG response: {payload!r}") from exc

  async def set_temperature(self, temperature: float) -> None:
    if not 20 <= temperature <= 45:
      raise ValueError("FilterMax temperature setpoint must be between 20 and 45 °C")
    value = f"{temperature:g}"
    await self._command(f"TS {value}", timeout=10)
    logger.info("[%s] temperature setpoint set to %.1f °C", self.name, temperature)

  async def deactivate_temperature_control(self) -> None:
    await self._command("TS 0", timeout=10)
    logger.info("[%s] temperature control deactivated", self.name)

  async def start_shaking(
    self,
    pattern: ShakePattern,
    speed: ShakeSpeed,
    duration: int = 5,
  ) -> None:
    if duration < 1:
      raise ValueError("duration must be at least 1")
    pattern_code, pattern_parameter = _SHAKE_WIRE[pattern]
    speed_parameter = _SHAKE_SPEED_WIRE[speed]
    await self._command(
      f"SHAKE {pattern_code} {pattern_parameter} 3 {speed_parameter} {duration}",
      timeout=duration + 30,
    )
    logger.info(
      "[%s] completed %s/%s shaking for %d seconds",
      self.name,
      pattern,
      speed,
      duration,
    )

  async def stop_shaking(self) -> None:
    if self._read_active:
      await self.cancel_read()
      return
    try:
      await self._command("STOP", timeout=10)
    except FilterMaxReadCancelled:
      return

  async def cancel_read(self) -> None:
    if not self._read_active:
      return
    self._cancel_event.set()
    await self._read_done.wait()

  def _require_catalog(self) -> FilterSlideCatalog:
    if self.filter_slide_catalog is None:
      raise FilterMaxCatalogError(
        "Measurement methods require FilterSlideCatalog.from_softmax_xml(...)"
      )
    return self.filter_slide_catalog

  @staticmethod
  def _plate_command(plate: PlateGeometry, read_height: float) -> str:
    values = (
      _hundredths(plate.length),
      _hundredths(plate.width),
      _hundredths(plate.height),
      _hundredths(plate.well_depth),
      _hundredths(plate.left_column_offset),
      _hundredths(plate.top_row_offset),
      plate.columns,
      plate.rows,
      _hundredths(plate.column_spacing),
      _hundredths(plate.row_spacing),
      _hundredths(plate.well_size_x),
      _hundredths(plate.well_size_y),
      0,
      300,
      0,
      _hundredths(read_height),
      1,
      1,
    )
    return "PLATE Temp " + " ".join(str(value) for value in values)

  async def _send_filter_catalog(self, kind: FilterKind, slide_id: int) -> None:
    catalog = self._require_catalog()
    filters = catalog.filters_for_slide(kind, slide_id)
    if not filters:
      raise FilterMaxCatalogError(f"Catalog has no {kind} slide ID {slide_id}")
    code = "A" if kind == "excitation" else "B"
    fields = [f"SF {code}", str(slide_id)]
    for item in filters:
      fields.extend((str(item.wavelength), str(item.bandwidth), str(item.apply_to)))
    await self._command(" ".join(fields), timeout=10)

  @staticmethod
  def _row_name(index: int) -> str:
    if not 0 <= index < 26:
      raise ValueError("FilterMax driver currently supports plate rows A-Z")
    return chr(ord("A") + index)

  @classmethod
  def _normalize_wells(
    cls,
    wells: Optional[Sequence[str]],
    plate: PlateGeometry,
  ) -> Tuple[Tuple[str, ...], int, int]:
    if wells is None:
      normalized = tuple(
        f"{cls._row_name(row)}{column + 1}"
        for row in range(plate.rows)
        for column in range(plate.columns)
      )
    else:
      normalized = tuple(dict.fromkeys(well.upper() for well in wells))
      if not normalized:
        raise ValueError("wells must not be empty")
    row_indices = []
    for well in normalized:
      match = _WELL_RE.match(well)
      if match is None or len(match.group("row")) != 1:
        raise ValueError(f"Invalid well name {well!r}")
      row = ord(match.group("row")) - ord("A")
      column = int(match.group("column")) - 1
      if not 0 <= row < plate.rows or not 0 <= column < plate.columns:
        raise ValueError(f"Well {well!r} is outside the {plate.rows}x{plate.columns} plate")
      row_indices.append(row)
    return normalized, min(row_indices), max(row_indices)

  @staticmethod
  def _parse_values(payload: str) -> List[float]:
    if not payload.startswith("+ "):
      raise FilterMaxProtocolError(f"Expected measurement data, received {payload!r}")
    try:
      return [float(value) for value in payload[2:].split()]
    except ValueError as exc:
      raise FilterMaxProtocolError(f"Malformed measurement values: {payload!r}") from exc

  async def _receive_measurement_messages(
    self,
    command: str,
    count: int,
    timeout: float,
  ) -> List[FilterMaxMessage]:
    await self._transport.send_request(command)
    messages: List[FilterMaxMessage] = []
    try:
      for index in range(count):
        messages.append(
          await self._transport.receive_message(
            timeout,
            command=command,
            cancel_event=self._cancel_event if index == 0 else None,
          )
        )
    except _CancelRequested:
      await self._transport.send_request("STOP")
      try:
        await self._transport.receive_message(15, command="STOP")
      except FilterMaxReadCancelled as error:
        self._errors.append(error)
        raise
      raise FilterMaxProtocolError("STOP did not return the captured E140 cancellation response")
    except FilterMaxDeviceError as error:
      self._errors.append(error)
      raise
    return messages

  async def _wait_until(self, target: float) -> None:
    delay = target - time.monotonic()
    if delay <= 0:
      return
    try:
      await asyncio.wait_for(self._cancel_event.wait(), timeout=delay)
    except asyncio.TimeoutError:
      return
    raise FilterMaxReadCancelled(140, "cancelled between kinetic cycles", "STOP")

  async def _recover_failed_read(self) -> None:
    """Stop an incomplete firmware read before attempting tray recovery."""

    try:
      await self._transport.exchange("STOP", timeout=15)
    except FilterMaxReadCancelled:
      logger.info("[%s] stopped incomplete FilterMax read", self.name)
    except FilterMaxDeviceError as error:
      self._errors.append(error)
      logger.warning("[%s] FilterMax read recovery returned: %s", self.name, error)

  async def _prepare_measurement(
    self,
    plate: PlateGeometry,
    *,
    kind: FilterKind,
    slide_id: int,
    read_height: float,
    shaking: Optional[ShakingSettings],
  ) -> None:
    slides = await self.get_filter_slides()
    installed = slides.excitation_slide_id if kind == "excitation" else slides.emission_slide_id
    if installed != slide_id:
      raise FilterMaxSlideNotInstalledError(
        f"Required {kind} slide ID {slide_id}, but installed ID is {installed}"
      )
    if shaking is not None:
      await self.move_plate_tray_in()
      await self.start_shaking(
        shaking.pattern,
        shaking.speed,
        duration=shaking.duration,
      )
    await self._command(self._plate_command(plate, read_height), timeout=10)
    if plate.orientation == "portrait":
      await self._command("SHIFT", timeout=10)
    await self._send_filter_catalog(kind, slide_id)
    await self.move_plate_tray_in()

  @staticmethod
  def _scan_layout(settings: WellScanSettings) -> Tuple[Tuple[int, float, float], ...]:
    density = settings.density
    spacing = settings.point_spacing
    center = (density - 1) / 2
    if settings.pattern == "horizontal":
      return tuple((index, (index - center) * spacing, 0.0) for index in range(density))
    radius = density / 2
    points = []
    for y_index in range(density):
      for x_index in range(density):
        x = x_index - center
        y = y_index - center
        if x * x + y * y <= radius * radius:
          raw_index = y_index * density + x_index
          points.append((raw_index, x * spacing, y * spacing))
    return tuple(points)

  async def read_absorbance(
    self,
    plate: PlateGeometry,
    wavelength: int,
    *,
    wells: Optional[Sequence[str]] = None,
    reference_wavelength: Optional[int] = None,
    slide_id: Optional[int] = None,
    filter_position: Optional[int] = None,
    reference_filter_position: Optional[int] = None,
    kinetic: Optional[KineticTiming] = None,
    shaking: Optional[ShakingSettings] = None,
    well_scan: Optional[WellScanSettings] = None,
  ) -> List[AbsorbanceResult]:
    catalog = self._require_catalog()
    primary = catalog.resolve(
      kind="excitation",
      technique="absorbance",
      wavelength=wavelength,
      slide_id=slide_id,
      position=filter_position,
    )
    if reference_wavelength is not None:
      reference = catalog.resolve(
        kind="excitation",
        technique="absorbance",
        wavelength=reference_wavelength,
        slide_id=primary.slide_id,
        position=reference_filter_position,
      )
    else:
      reference = None
    selected_wells, first_row, last_row = self._normalize_wells(wells, plate)
    selected = set(selected_wells)
    row_count = last_row - first_row + 1
    grid_x, grid_y = well_scan.grid_shape if well_scan is not None else (1, 1)
    scan_layout = self._scan_layout(well_scan) if well_scan is not None else ((0, 0.0, 0.0),)
    raw_point_count = grid_x * grid_y
    scan_code = 4 if well_scan is not None else 0
    timing = kinetic or KineticTiming(interval=1, reads=1)
    results: List[AbsorbanceResult] = []

    async with self._operation_lock:
      self._cancel_event.clear()
      self._read_done.clear()
      self._read_active = True
      completed = False
      try:
        await self._prepare_measurement(
          plate,
          kind="excitation",
          slide_id=primary.slide_id,
          read_height=plate.absorbance_z,
          shaking=shaking,
        )
        kinetic_started = time.monotonic()
        for cycle in range(timing.reads):
          if cycle:
            if shaking is not None and shaking.between_reads:
              await self.start_shaking(shaking.pattern, shaking.speed, shaking.duration)
            await self._wait_until(kinetic_started + cycle * timing.interval)
          # SoftMax reports kinetic timepoints on the requested schedule, even when a read
          # overruns its interval. Preserve that protocol-level meaning instead of exposing
          # transport and calibration latency as kinetic elapsed time.
          elapsed_time = cycle * timing.interval if kinetic is not None else 0.0
          remaining = timing.reads - cycle
          if kinetic is not None:
            mode = 3
            kinetic_flag = 2
          elif reference is not None:
            mode = 1
            kinetic_flag = 0
          else:
            mode = 0
            kinetic_flag = 0
          wavelengths = (
            f"2 {primary.wavelength} {reference.wavelength}"
            if reference is not None
            else f"1 {primary.wavelength}"
          )
          orientation_code = 4 if plate.orientation == "portrait" else 3
          command = (
            f"ABS {mode} {wavelengths} {first_row + 1} {last_row + 1} "
            f"{grid_x} {grid_y} {scan_code} 0 0 0 0 {orientation_code} 1 "
            f"{remaining} {kinetic_flag} O e INFO"
          )
          timestamp = time.time()
          final_cycle = cycle == timing.reads - 1
          messages = await self._receive_measurement_messages(
            command,
            row_count * grid_y + int(final_cycle),
            timeout=max(60, raw_point_count * 3),
          )
          data_messages = messages[:-1] if final_cycle else messages
          data = empty_plate_data(plate.rows, plate.columns)
          scan_points: Dict[str, Tuple[WellScanPoint, ...]] = {}
          for row_offset in range(row_count):
            row = first_row + row_offset
            raw_values_by_column: List[List[float]] = [[] for _ in range(plate.columns)]
            for scan_y in range(grid_y):
              message = data_messages[row_offset * grid_y + scan_y]
              values = self._parse_values(message.payload)
              expected = plate.columns * grid_x
              if len(values) != expected:
                raise FilterMaxProtocolError(
                  f"Expected {expected} absorbance values for row {row + 1}, got {len(values)}"
                )
              for column in range(plate.columns):
                raw_values_by_column[column].extend(values[column * grid_x : (column + 1) * grid_x])
            for column in range(plate.columns):
              well = f"{self._row_name(row)}{column + 1}"
              raw_points = raw_values_by_column[column]
              converted = [raw_points[index] / 1000 for index, _, _ in scan_layout]
              if well not in selected:
                continue
              data[row][column] = sum(converted) / len(converted)
              if well_scan is not None:
                scan_points[well] = tuple(
                  WellScanPoint(x=x, y=y, value=value)
                  for (_, x, y), value in zip(scan_layout, converted)
                )
          temperature = await self.get_temperature()
          results.append(
            AbsorbanceResult(
              data=data,
              wavelengths=(
                (primary.wavelength, reference.wavelength)
                if reference is not None
                else (primary.wavelength,)
              ),
              reference_subtracted=reference is not None,
              temperature=temperature,
              timestamp=timestamp,
              elapsed_time=elapsed_time,
              scan_points=scan_points,
            )
          )
        completed = True
      finally:
        if not completed:
          with contextlib.suppress(Exception):
            await self._recover_failed_read()
        with contextlib.suppress(Exception):
          await self.move_plate_tray_out()
        self._read_active = False
        self._read_done.set()
    return results

  async def read_luminescence(
    self,
    plate: PlateGeometry,
    *,
    wells: Optional[Sequence[str]] = None,
    channels: int = 1,
    slide_id: Optional[int] = None,
    filter_position: Optional[int] = None,
    integration: float = 0.4,
    read_height: float = 1.0,
    kinetic: Optional[KineticTiming] = None,
    shaking: Optional[ShakingSettings] = None,
  ) -> List[LuminescenceResult]:
    if plate.orientation != "landscape":
      raise FilterMaxUnsupportedOperationError(
        "Portrait orientation has been captured only for absorbance measurements"
      )
    if channels not in (1, 2):
      raise ValueError("channels must be 1 or 2")
    if integration <= 0:
      raise ValueError("integration must be positive")
    catalog = self._require_catalog()
    lum_filter = catalog.resolve(
      kind="emission",
      technique="luminescence",
      wavelength=0,
      slide_id=slide_id,
      position=filter_position,
    )
    selected_wells, first_row, last_row = self._normalize_wells(wells, plate)
    selected = set(selected_wells)
    row_count = last_row - first_row + 1
    timing = kinetic or KineticTiming(interval=1, reads=1)
    integration_microseconds = int(round(integration * 1_000_000))
    results: List[LuminescenceResult] = []

    async with self._operation_lock:
      self._cancel_event.clear()
      self._read_done.clear()
      self._read_active = True
      completed = False
      try:
        await self._prepare_measurement(
          plate,
          kind="emission",
          slide_id=lum_filter.slide_id,
          read_height=read_height,
          shaking=shaking,
        )
        kinetic_started = time.monotonic()
        for cycle in range(timing.reads):
          if cycle:
            if shaking is not None and shaking.between_reads:
              await self.start_shaking(shaking.pattern, shaking.speed, shaking.duration)
            await self._wait_until(kinetic_started + cycle * timing.interval)
          # The instrument starts an overdue cycle immediately, but its result still belongs
          # to the nominal kinetic timepoint (the behavior observed in SoftMax captures).
          elapsed_time = cycle * timing.interval if kinetic is not None else 0.0
          remaining = timing.reads - cycle
          mode = 3 if kinetic is not None else (1 if channels == 2 else 0)
          wavelengths = "2 0 0" if channels == 2 else "1 0"
          command = (
            f"LUM {mode} {wavelengths} {first_row + 1} {last_row + 1} 1 1 0 0 0 "
            f"{integration_microseconds} 0 3 1 {remaining} 0 O INFO"
          )
          timestamp = time.time()
          final_cycle = cycle == timing.reads - 1
          messages = await self._receive_measurement_messages(
            command,
            row_count * channels + int(final_cycle),
            timeout=90,
          )
          data_messages = messages[:-1] if final_cycle else messages
          for channel in range(channels):
            data = empty_plate_data(plate.rows, plate.columns)
            for row_offset in range(row_count):
              message = data_messages[channel * row_count + row_offset]
              values = self._parse_values(message.payload)
              if len(values) != plate.columns:
                raise FilterMaxProtocolError(
                  f"Expected {plate.columns} luminescence values, got {len(values)}"
                )
              row = first_row + row_offset
              for column, value in enumerate(values):
                well = f"{self._row_name(row)}{column + 1}"
                if well in selected:
                  data[row][column] = value
            results.append(
              LuminescenceResult(
                data=data,
                channel=channel + 1,
                temperature=await self.get_temperature(),
                timestamp=timestamp,
                elapsed_time=elapsed_time,
              )
            )
        completed = True
      finally:
        if not completed:
          with contextlib.suppress(Exception):
            await self._recover_failed_read()
        with contextlib.suppress(Exception):
          await self.move_plate_tray_out()
        self._read_active = False
        self._read_done.set()
    return results

  async def _validate_unavailable_fluorescence_filters(
    self,
    technique: str,
    excitation_wavelength: int,
    emission_wavelength: int,
    excitation_slide_id: Optional[int],
    emission_slide_id: Optional[int],
  ) -> None:
    catalog = self._require_catalog()
    excitation = catalog.resolve(
      kind="excitation",
      technique=technique,  # type: ignore[arg-type]
      wavelength=excitation_wavelength,
      slide_id=excitation_slide_id,
    )
    emission = catalog.resolve(
      kind="emission",
      technique=technique,  # type: ignore[arg-type]
      wavelength=emission_wavelength,
      slide_id=emission_slide_id,
    )
    installed = await self.get_filter_slides()
    if installed.excitation_slide_id != excitation.slide_id:
      raise FilterMaxSlideNotInstalledError(
        f"Required excitation slide ID {excitation.slide_id}, but installed ID is "
        f"{installed.excitation_slide_id}"
      )
    if installed.emission_slide_id != emission.slide_id:
      raise FilterMaxSlideNotInstalledError(
        f"Required emission slide ID {emission.slide_id}, but installed ID is "
        f"{installed.emission_slide_id}"
      )
    raise FilterMaxUnsupportedOperationError(
      f"{technique} measurement framing requires a live capture with excitation slide "
      f"ID {excitation.slide_id}; no command was inferred"
    )

  async def read_fluorescence(
    self,
    plate: PlateGeometry,
    excitation_wavelength: int,
    emission_wavelength: int,
    *,
    excitation_slide_id: Optional[int] = None,
    emission_slide_id: Optional[int] = None,
  ) -> List[FluorescenceResult]:
    del plate
    await self._validate_unavailable_fluorescence_filters(
      "fluorescence",
      excitation_wavelength,
      emission_wavelength,
      excitation_slide_id,
      emission_slide_id,
    )
    raise AssertionError("unreachable")

  async def read_time_resolved_fluorescence(
    self,
    plate: PlateGeometry,
    excitation_wavelength: int,
    emission_wavelength: int,
    *,
    excitation_slide_id: Optional[int] = None,
    emission_slide_id: Optional[int] = None,
  ) -> List[TimeResolvedFluorescenceResult]:
    del plate
    await self._validate_unavailable_fluorescence_filters(
      "time_resolved_fluorescence",
      excitation_wavelength,
      emission_wavelength,
      excitation_slide_id,
      emission_slide_id,
    )
    raise AssertionError("unreachable")

  async def read_fluorescence_polarization(
    self,
    plate: PlateGeometry,
    excitation_wavelength: int,
    emission_wavelength: int,
    *,
    excitation_slide_id: Optional[int] = None,
    emission_slide_id: Optional[int] = None,
  ) -> List[FluorescencePolarizationResult]:
    del plate
    await self._validate_unavailable_fluorescence_filters(
      "fluorescence_polarization",
      excitation_wavelength,
      emission_wavelength,
      excitation_slide_id,
      emission_slide_id,
    )
    raise AssertionError("unreachable")
