# ABI 7500 Fast Dx

```{device-card} thermo-fisher-abi7500-fast-dx
```

```{warning}
Integration is incomplete. `pylabrobot.thermo_fisher.ABI7500FastDx` implements experimental USB
connection, identity and temperature/status queries, checked directly from Ubuntu on one G2.10
instrument. It cannot start or cancel a thermal run. Live acquisition and calibrated fluorescence
remain unimplemented and unverified. See the [hardware validation report](hardware-validation.md).
```

The driver uses `pylabrobot.io.usb.USB` directly, with no vendor DLL dependency. Thermal runs,
live camera acquisition, and per-well fluorescence remain under development. Ct analysis, assay
interpretation, firmware updates, and recalibration
are outside its scope. A completed two-cycle vendor run and a cancelled run provide USB captures,
62 original camera frames, and matching vendor outputs. The image crop matches the native reference
routine exactly. A thermal-program codec reproduces the captured program upload and native
cycle/setpoint encoder outputs. Well geometry, exposure processing, calibration, run reconciliation,
and live PLR run execution remain unvalidated.

```{toctree}
:maxdepth: 1

hello-world
protocol
hardware-validation
capture
```
