"""Deprecated import location for the current FilterMax F5 v1 driver."""

import warnings

from pylabrobot.molecular_devices.filtermax import FilterMaxF5, FilterSlideCatalog

warnings.warn(
  "pylabrobot.legacy.plate_reading.molecular_devices.filtermax_f5 is deprecated; "
  "use pylabrobot.molecular_devices.filtermax instead.",
  DeprecationWarning,
  stacklevel=2,
)

__all__ = ["FilterMaxF5", "FilterSlideCatalog"]
