# ABI 7500 Fast Dx

```{device-card} thermo-fisher-abi7500-fast-dx
```

```{warning}
Integration is incomplete. PLR provides offline packet codecs, camera reconstruction, and saved
EDS filter-data reading. There is no `pylabrobot.thermo_fisher.ABI7500FastDx` control class yet.
Do not use the packet examples to send commands to an instrument.
```

The intended driver will use `pylabrobot.io.USB` directly for thermal runs, original camera frames,
and per-well fluorescence. Ct analysis, assay interpretation, firmware updates, and recalibration
are outside its scope. A completed two-cycle vendor run and a cancelled run provide USB captures,
62 original camera frames, and matching vendor outputs. The image crop matches the native reference
routine exactly. Well geometry, exposure processing, calibration, run reconciliation, and direct
Ubuntu control remain unvalidated.

```{toctree}
:maxdepth: 1

hello-world
protocol
capture
```
