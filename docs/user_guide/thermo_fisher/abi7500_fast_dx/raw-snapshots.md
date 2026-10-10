# Experimental raw snapshots

```{warning}
`capture_frame()` completed snapshots directly from Ubuntu at native filter indices 0, 1 and 5.
The illumination session verified lamp-on signals at filter 1 (0.256 seconds) and filter 5
(0.090 seconds, saturated). Optical fault recovery remains hardware unverified. This engineering
snapshot API does not synchronize exposures with a thermal experiment. See the [hardware report](hardware-validation.md).
Obtain the hardware approval required by your local setup before calling it.
```

Snapshots were checked from Ubuntu with Windows disconnected.
Snapshots use the current lamp and filter settings. The operation does not move either the plate or
filter, switch the lamp, start a thermal program, or change the current temperature setpoints.
It may produce a dark image; the completed test verifies camera control and complete frame transfer.
It does not require a fresh reaction plate or calibration plates.

`capture_frame(acquisition_id, exposure=0.256)` checks the camera identity, geometry, readiness,
thermal inactivity and stationary filter before reserving the ID in the existing `run_journal`.
Every thermal run in that journal must already be resolved. The implementation supports the
reference C9254-03 camera firmware 2.20, thermal firmware G2.10, and 321×245 wire geometry.
Exposure is in seconds, limited to 0.005–1.048, using the reference millisecond conversion.

```python
# Hardware operation: only execute in an approved session, after instrument.setup().
acquisition = await instrument.capture_frame("plr-snapshot-001", exposure=0.256)
assert acquisition.state == "complete"
frame = acquisition.frame
assert frame is not None
Path("snapshot-original.bin").write_bytes(frame.wire_data)
Path("snapshot-image-u16le.bin").write_bytes(frame.image_bytes)
```

The result retains all 157,312 transfer bytes, including the margins and opaque 22-byte tail.
The reference crop returns 320×240 unsigned little-endian 16-bit pixels. Metadata includes the
requested exposure, original millisecond echo, readiness replies, instrument identities, host
timestamps, and full thermal/optical replies before and after transfer. Native filter indices
are not dye labels; lamp enable and native power words are exposed with the original bytes.
See [optical control](optics-and-measurements.md) for the separate setters and their limits.
Host timestamps do not establish synchronization
with a thermal cycle. No calibration, background subtraction, exposure combination, well mapping
or Ct analysis is applied.

Reusing the same acquisition ID and exposure returns the stored record without USB I/O, even
after restarting Python. Reusing the ID with a different exposure is rejected. Use the same
persistent local journal as thermal runs; keep it outside Git and synchronized folders.

An interrupted operation faults the connection and leaves an `uncertain` record. That state
does not establish whether an exposure actually occurred. `get_acquisition(id)` reads the
record offline, including any data received before a handled interruption. A full frame can
be present even when final status failed or changed. A process crash may lose bytes not yet
checkpointed, but the durable intent still prevents a repeated CC for that ID. Do not delete
the record, switch journals, or change IDs merely to retry. Stop and reopen the connection
before further I/O; a new ID explicitly requests a separate exposure. There is no automatic
resume or second exposure. Python task cancellation closes out the host operation, waits for
in-flight USB calls, and records uncertainty; it does not send an instrument exposure abort.

Offline [camera processing](optics-and-measurements.md) supports both diagnostic pixel statistics
and reconstruction of the recorded three-exposure vendor measurements with explicit calibration
files. All 960 saved integers matched for the reference acquisition. A single snapshot alone does
not supply the three exposures or establish synchronization, calibration validity or assay performance.
