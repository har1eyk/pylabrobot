# Dye signals and ROX normalization

`unmix_well()` and `unmix_collection()` separate overlapping dye signals from already
corrected filter measurements. They retain signed components, calibration provenance,
fit diagnostics and quality flags. With an explicitly selected passive reference,
they also calculate the reporter/reference ratio, Rn. Everything runs offline;
these functions do not open USB or modify instrument calibration.

```{warning}
This is a numerically verified processing path, not an assay validation or confirmation
of current calibration. An EDS calibration's saved pass flag, timestamp or wildcard
instrument identifier does not establish its suitability for a new plate.
EvaGreen, SYBR, FAM and fluorescein are not interchangeable calibration names.
No baseline subtraction, ΔRn, Ct, concentration or positive/negative call is calculated.
```

## Processing order

Start with `CameraFilterResult` or `UniformityCorrectedData` from the existing
[measurement pipeline](optics-and-measurements.md). Native ring/background subtraction,
exposure correction and uniformity must already have been applied exactly once.
The spectral stage does not repeat those operations.

For each well, the calculation:

1. Reads the explicitly selected dyes' five-channel spectra from `DyeCalibration`,
   retaining the original INI bytes and SHA-256, model, instrument/scanner identifiers,
   dye-specific timestamps and saved pass status.
2. Normalizes each dye spectrum by its maximum over all five saved filters, then
   selects the explicitly enabled filters.
3. Solves the linear least-squares system using binary32 SVD arithmetic, including
   separate rounding of products and sums. Negative fitted components are retained.
4. Reports the condition number and residual standard error. The residual has no
   degrees of freedom when the number of filters equals the number of dyes, so its
   value is `None` for a square system.
5. Divides the separated reporter signal by the separated passive-reference signal,
   rounding the result to binary32. The recovered 7500-family instrument normalization
   factor is unity. Selecting no passive reference leaves dye signals available but
   does not label them normalized Rn.

Filters are **saved channels 1–5**, the reverse of physical wheel positions. Include
every fluorescent component specified by the method, including fluorescent quenchers
where applicable. For bitwise vendor replay, preserve the dye order recorded in
`multicomponent_data.txt`; permuting the same columns can change floating-point rounding.
PLR never infers dye identity from the strongest channel or silently drops a missing filter.

## Replay a saved collection

This example uses the saved `SYBR`, `ROX` order of the archived green diagnostic.
Select the actual names and order for another experiment.

```python
from pylabrobot.thermo_fisher.abi7500_fast_dx import (
  apply_uniformity,
  read_eds_dye_calibration,
  read_eds_filter_data,
  read_eds_uniformity,
  unmix_collection,
)

path = "vendor-complete.eds"
arrays = read_eds_filter_data(path)
uniformity = read_eds_uniformity(path)
dyes = (
  read_eds_dye_calibration(path, "SYBR"),
  read_eds_dye_calibration(path, "ROX"),
)

# Group explicitly: do not combine different cycles or collection points.
first = arrays[0]
point = (first.stage, first.cycle, first.collection_point)
filters = tuple(
  apply_uniformity(array, uniformity)
  for array in arrays
  if (array.stage, array.cycle, array.collection_point) == point
)
measurements = unmix_collection(
  filters, dyes, reporter="SYBR", passive_reference="ROX", review_wells=(73,)
)
report = measurements[0].report()
print(report["components"], report["unrestricted_rn"], report["flags"])
```

`review_wells=(73,)` preserves the unresolved G1 concern in this project's current
comparison. It is an explicit caller decision, not a permanent built-in correction.
`DyeCalibration(..., review_wells=...)` can separately carry repaired or otherwise
flagged calibration wells from their accompanying report. The INI does not encode
all of those decisions; retain the report and original images.

Saved EDS filter arrays contain no saturation status, so these measurements have
`saturation_unknown`. Their finite `unrestricted_rn` supports numerical replay,
but guarded `rn` is `None`. To process a completed PLR acquisition with known
saturation, pass the tuple of `CameraFilterResult` objects for the same collection
point to `unmix_collection()`. It propagates each well's selected-exposure saturation.
Its input identifier includes frame hashes, commanded exposures, masks, background
and uniformity hashes. Returned records retain the acquisition coordinates and
pure-dye calibration objects.

`unmix_well()` accepts a `WellFilterSignals` object when inputs already exist in
another offline workflow. `FilterSignal.value=None` explicitly represents missing
data; zero remains a measured zero. Supply a meaningful source identifier.
Well and stage are one-based. Cycle and collection-point indices are preserved;
zero is accepted for older serialized EDS coordinates without silently renumbering them.
This does not broaden the separate saved-array reader's supported member-name format.

## Quality and failure handling

`components` preserves signed diagnostic values. `unrestricted_rn` preserves a finite
vendor-style ratio even when quality flags make it unsuitable for use. `rn` is provided
only when none of the blocking flags below applies:

| Flag | Meaning |
| --- | --- |
| `missing_filter` | An explicitly enabled filter is absent or has no value; components are unavailable. |
| `saturated` | A selected input exposure is saturated. |
| `saturation_unknown` | The input lacks saturation metadata. |
| `negative_component` | At least one fitted dye contribution is negative; it is retained without clipping. |
| `nonpositive_reference` | The reference is zero or negative. Zero does not produce an infinite ratio. |
| `low_reference` | The positive reference is below the explicit `minimum_reference` floor. |
| `nonfinite_ratio` | The ratio cannot be represented as a finite binary32 value. |
| `invalid_spectral_calibration` | A spectrum is empty, singular, ill-conditioned or otherwise cannot be solved. |
| `calibration_failed` | A failed historical calibration was explicitly loaded for diagnostic replay. |
| `review_required` | The caller marked this well or one of its calibration spectra for review. |
| `no_passive_reference` | Dye signals were requested without a reference; no normalized ratio is produced. |

`calibration_identity_unspecified` is informational: the saved instrument/scanner ID is
`null` or `*`. The caller must establish the file's association and current suitability.
Known conflicting IDs or model types are rejected. A clear flag list does not certify
calibration age, chemical suitability, acquisition ownership or assay performance.

The default `minimum_reference=10.0` comes from the vendor normalization component.
It is a signal-unit threshold, not a ROX concentration or a limit of detection.
PLR's `max_condition=10000.0` is a numerical safeguard, not a recovered vendor
calibration acceptance criterion. Both settings are retained in each report.
No regularization, automatic dye substitution or well-specific intensity adjustment occurs.

Failed calibration files are rejected by default. `allow_failed=True` on
`DyeCalibration` or `read_eds_dye_calibration()` is exclusively for diagnostic replay:
the original failed status remains visible and guarded `rn` stays unavailable.

## Saved-data validation

Four archived experiments were replayed from their own saved integer filter arrays,
uniformity factors and pure-dye spectra. Expected values came independently from the
vendor's saved `multicomponent_data.txt` and `analysis_result.txt`.

| Archive | Exact separated dye values | Exact residuals | Exact normalized Rn |
| --- | ---: | ---: | ---: |
| One-point SYBR/ROX diagnostic | 192 / 192 | 96 / 96 | 96 / 96 |
| Eight-point ROX-only settled acquisition | 768 / 768 | 768 / 768 | No passive reference |
| Bundled Standard Curve Example, FAM/TAMRA/ROX | 2,880 / 2,880 | 960 / 960 | 960 / 960 |
| Bundled Comparative Ct Example, FAM/ROX | 7,680 / 7,680 | 3,840 / 3,840 | 3,840 / 3,840 |
| **Total** | **11,520 / 11,520** | **5,664 / 5,664** | **4,896 / 4,896** |

The ROX-only archive's 768 values labeled Rn by the vendor were unnormalized signals;
those also matched exactly and remain explicitly unnormalized in PLR. Both bundled
examples carry failed calibration metadata and were loaded only for diagnostic replay.
All archived filter arrays have unknown saturation status. None of these numerical
comparisons promotes a historical file to an accepted current calibration.

Small excerpts cover early/late cycles, one to three dyes, G1 and weak/negative
reference cases in automated tests. Full sources, original calibration files and the
complete comparison remain outside Git in `abi7500-research/analyses/multicomponent-001/`.
The underlying float arithmetic needs no NumPy, Java, vendor DLL or hardware connection.
Current-instrument green-reporter calibration and mixed reporter/reference performance
remain hardware-validation work.
