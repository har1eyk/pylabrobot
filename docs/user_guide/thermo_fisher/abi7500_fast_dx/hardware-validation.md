# ABI 7500 Fast Dx hardware validation

## Direct Ubuntu connection and status

Session `abi7500-plr-observe-001` exercised the PLR USB driver on one ABI 7500 Fast Dx on
2026-10-09, from 17:50:35.999 to 17:50:42.196 UTC. The USB cable was connected directly to Ubuntu
24.04, with the Windows control computer disconnected. No vendor DLL or Windows process was used.

| Component | Observed value |
|---|---|
| Python | 3.13.5 |
| Thermal firmware | `G2.10` |
| Camera identification reply | `Hamamatsu,C9254-03,2.20,1.05,2.19,007006` |
| USB product descriptor | `Hamamatsu C9254-01` |
| USB VID:PID | `0c2b:0300` |
| Endpoints | Bulk OUT `0x06`, bulk IN `0x85` |
| Wire image geometry | 321 × 245 |
| Source base | `0356137641d37e82be38306b1c8ecb3d348482e2` plus local driver changes |

The camera reply and USB product descriptor identify different interfaces and are preserved
separately. The complete source used for the test is archived alongside its I/O transcript;
`audit.json` records source-file hashes. Later warning/docstring changes do not alter the tested
command sequence.

| Operation | Expected result | Observed result |
|---|---|---|
| `setup()` | Ready camera, application-mode thermal controller, valid identity | Succeeded |
| `get_identity()` | Camera description and thermal version | Values above, with full replies retained |
| Five `get_status()` calls | Complete, matched 50-byte status payloads | All succeeded; sequence inactive |
| Keepalives | `C:` writes serialized with queries | Ten writes recorded |
| `stop()` | Close USB and terminate keepalives | Returned; process exited with code 0 |
| Thermal-run start/cancellation | Not tested | Outside this connection/status session |
| Live image acquisition and well fluorescence | Not tested | Outside this connection/status session |

The status replies reported sample temperature 25.0 °C, block temperature 24.9–25.0 °C, ambient
temperature 24.5 °C, and cover temperature 105.3–104.8 °C. These are controller readings, not
independent thermometer measurements. The session sent no program upload, heating, lamp, filter,
plate-motion, or image-acquisition command. An inactive sequence did not imply a cool cover.

Elapsed time advanced from 11557 to 11562 seconds while the sequence remained inactive. Remaining
hold time stayed at 14 seconds. These counters alone cannot establish run activity or completion.

## Evidence and limits

The saved transcript contains 364 PLR transport records: 193 writes and 171 reads. All eight thermal
requests have matched replies with valid lengths, checksums, command/address/sequence fields and
zero firmware error fields. The only thermal commands were application-mode query `0x2f`, version
query `0x0a`, and extended status `0x29`. Their replies required 167 counted serial reads, including
zero-count reads and one-byte fragments with stale, nonzero padding.

Small exact boot/status exchanges and the five status payloads are retained in
`pylabrobot/thermo_fisher/abi7500_fast_dx/test_data/ubuntu_observation_vectors.json`. Offline tests
replay the USB transactions and check the inactive-state interpretation. Full artifacts remain
outside Git under `abi7500-research/captures/abi7500-plr-observe-001/`:

| Artifact | SHA-256 |
|---|---|
| `plr-io.json` | `bdc34452ac57fa272ba1edb1bce74a8f14da4964410c3d8163e1a7f4a99003c7` |
| `result.json` | `e27118bdc933a21c55047aecd77785f46bfa9645e5eea77a55f81d90497c66a0` |
| `observe_plr_connection.py` | `23e865376d629b42c5056db532148b046c1a06d276803fb77340909c4a1972e6` |

This is a PLR transport transcript, not a USBPcap or usbmon capture. Its byte reconstruction was
checked against expected lengths, packet checksums, and separately saved result payloads. The
session log file is empty; lifecycle completion was reported by the process console and exit code.

This test establishes connection and status communication on this instrument and firmware only.
It does not validate thermal control, cancellation, recovery after ambiguous failures, duplicate-run
prevention, other firmware, live acquisition, physical well orientation, calibration, or assay
performance. The device remains marked as incomplete and needing hardware testing.

## Direct Ubuntu thermal start and cancellation

Session `abi7500-plr-cancel-001` exercised one approved thermal-only run on the same instrument on
2026-10-09, from 19:10:47.720 to 19:15:37.985 UTC. The uploaded program contained one 35 °C,
60-second hold using the captured Standard/20 µL/block-2/tube-0 configuration. Windows remained
physically disconnected. No optical acquisition, firmware change or recalibration was performed.
The source base was `fa267dd35b453ada6f2a3c96d93cae62f3193717` plus local run-control changes;
the executed sources and their hashes are archived with the session.

| Operation | Observed result |
|---|---|
| Prepare `0x1b` and upload `0x12` | Each sent once and acknowledged |
| Plate query/load `0x73` / `0x71` | Unloaded 0, moving 1, loaded 2; one load, about 5.7 seconds to confirmed loading |
| Start `0x13` | Sent once; acknowledged at 19:10:57.103 UTC |
| Startup | State 3, inactive sequence phase, old run timestamp/history; sample 25 °C while cover warmed |
| Reconnection | Closed/reopened PLR USB during startup; same journal and run ID, no repeated upload or Start |
| Active progress | Fresh start record and ramping phase observed at 19:15:29.657 UTC, about 273 seconds after Start acknowledgement |
| Stop `0x07` | Sent once at 19:15:30.265 UTC; acknowledged, followed by confirmed inactivity |
| Idle target `0x22` | `00fa` (25 °C) sent once after inactivity and acknowledged |
| History | Method `plr-cancel-001`, user `PLR`, matching fresh start record, aborted outcome, duration 2 seconds |
| Final checkpoint | `cancelled`, cancellation requested, idle target acknowledged; retained in the persistent journal |
| Final status and close | Inactive, sample 25.2 °C, block 24.5 °C, cover 103.6 °C; USB closed cleanly |

The first research runner imposed a 25-second observation window after Start, which was too short
for cover warmup. It recorded an observation error, retained the acknowledged `starting` checkpoint,
and closed USB at 19:11:23.254 UTC. A continuation opened USB at 19:11:47 UTC and observed/cancelled
**the same run**, without a new start call or run ID. Both original result files are retained; the
initial file reports incomplete validation, while the continuation records the successful outcome.
This was a clean process/connection replacement, not recovery from a lost acknowledgement or
broken USB transfer. The driver needed no lifecycle changes to reconcile it.

The native run start record changed from `1a0a09000910010000` to `1a0a09010135220000`.
The new history labels and start record were visible during active ramping, with outcome code 253.
That observation disproves interpreting 253 as proof that no run started: the API reports
`no_result` until a final history outcome exists. During warmup, the old aborted history remained
visible and did not prematurely resolve the new run.

Cancellation occurred before the sample reached 35 °C. Across the captured queries, sample readings
were 25.0–25.4 °C, block readings 24.5–26.4 °C and cover readings 59.2–103.6 °C. These are controller
readings. This session does not verify setpoint attainment, the 60-second hold, temperature accuracy,
normal completion, startup cancellation, or recovery from communication faults. The plate was not
ejected, and cancellation did not cool the cover.

### Thermal-session evidence

The two transcripts contain 22,068 completed USB writes and 20,861 reads. All 701 thermal exchanges
have matched command/address/sequence fields, valid lengths/checksums and zero firmware errors.
There are exactly one each of prepare, upload, load, Start, Stop and idle-target commands. No optical
acquisition command was sent. A research recorder saved bytes directly as hex at the PLR USB
boundary; these are transport transcripts, not USBPcap or usbmon captures.

Full logs, hex transcripts, source snapshots, the initial and final closed-journal copies, and the
independent byte audit remain outside Git in
`abi7500-research/captures/abi7500-plr-cancel-001/`. An archive is saved in Seafile
`Temp/ABI-7500/abi7500-plr-cancel-001.zip`. The canonical live journal remains on local disk;
archived copies must not be restored to retry an experiment.

| Artifact | SHA-256 |
|---|---|
| `usb-hex.jsonl` | `06b755d6fa69bfc469990a31b692e4e5fe045386932b4531d4e7317382e13afb` |
| `continuation/usb-hex.jsonl` | `3f96fda0ff8decdd358bb4fc4aee769ed28b2d6b58fbe52a6ada9b79c326fe88` |
| `result.json` | `0297fa50028f12e987a3a9fa451525d7428201daea47e51ba9e3eb5b3914fbe2` |
| `continuation/result.json` | `5d2e69c3b90c1d23488cb087e1bb9d5db2332b10c27849871cc0950e6baacd8f` |

The small `test_data/ubuntu_run_vectors.json` fixture retains exact selected exchanges and
checkpoints. Regression tests replay their fragmented responses, preserve `starting` across a
journal reopen, reconcile the real custom labels, and distinguish active code 253 from a final
outcome. The device retains its incomplete status and hardware-testing flag for the remaining scope.

## Direct Ubuntu 35 °C, 60-second completion

Session `abi7500-plr-hold-001` executed the separately approved complete hold on 2026-10-09, from
20:40:00.118 to 20:41:32.583 UTC. It used the same G2.10 instrument, Standard/20 µL/block-2/tube-0
configuration and sealed plate, with Windows disconnected and no optics. The journal retained the
previous cancelled run and reserved the distinct ID `plr-hold-001`. The executed sources and their
hashes are preserved with the capture; no run-lifecycle code change was needed for this test.

| Operation | Observed result |
|---|---|
| Initial state | Inactive; sample/block 25.0 °C, cover 105.2 °C; prior cancelled history matched the journal |
| Plate state | Already loaded (2); no loading or ejection command sent |
| Program | One repetition of one 35 °C, 60-second hold; verified in the uploaded bytes |
| Start | One `0x13`, acknowledged at 20:40:03.804 UTC |
| Ramp | Fresh active status appeared at 20:40:04.798 UTC; the first bracketing observation crossed the startup boundary and remained `uncertain` |
| Hold entry | Holding phase at elapsed 13 seconds, 60 seconds remaining; sample estimate 34.6 °C |
| Hold progress | Remaining time counted down from 60 to 1; holding samples spanned 59.038 host seconds, ending at elapsed 72 |
| Completion | Transient inactive/state 6, then inactive/state 2 with matching completed history; `completed` checkpoint at 20:41:19.746 UTC |
| History | Method `plr-hold-001`, user `PLR`, outcome 0 (normal completion), duration 73 seconds including ramp |
| Final temperatures | Sample/block 35.0 °C, cover 104.7 °C; still inactive, USB closed cleanly |

The holding-phase sample estimates ranged from 34.6 to 35.2 °C, including initial settling, then
settled near 35.0 °C. Block readings ranged from 24.9 to 36.4 °C across ramp and hold, and cover
readings ranged from 104.6 to 105.4 °C. These controller readings and the 60-second programmed
countdown validate this thermal sequence's control and reporting, not independent temperature
accuracy or uniformity across wells. Polling does not resolve the exact subsecond hold boundaries.

The fresh run record was `1a0a09010312090000`, distinct from the previous run. History retained the
correct NUL-terminated method name even though bytes after the terminator contained part of the
longer preceding label. During completion, state 6 was treated as transitional/uncertain; no
unverified idle-state mapping was added. A later coherent status/history observation established
normal completion.

There was one prepare, one upload and one Start, with **no Stop or idle-target command**. Normal
completion retained the last 35 °C target during the subsequent observation period; it did not
request 25 °C or cool the cover. The completed checkpoint has `cancel_requested=False` and
`idle_temperature_set=False`. This differs from the explicit cancellation sequence's idle request.

### Completion-session evidence

All 213 thermal exchanges have valid lengths/checksums, matched command/address/sequence fields
and zero firmware error fields. The exact-hex transport transcript contains 6,877 completed USB
writes and 6,486 reads. The full evidence remains outside Git in
`abi7500-research/captures/abi7500-plr-hold-001/`, including `temperature-trajectory.json`, PNG/SVG
plots, logs, source snapshots, closed journals before/after, and `audit.json`. Its archive is saved
in Seafile `Temp/ABI-7500/abi7500-plr-hold-001.zip`.

| Artifact | SHA-256 |
|---|---|
| `usb-hex.jsonl` | `9992a2ee6533a4dd76cd579f41d05981104c9d5ecf9d5b6172c04b7478810e07` |
| `result.json` | `2bccdb76d1cfe5605827d1bf9cb4d9ea5adc91731feff0d6c2dd6899af04dcff` |

`test_data/ubuntu_hold_vectors.json` keeps selected original exchanges and observations for offline
regression tests. They cover startup/completion transitions, hold progress, matching completed
history and a durable result that cannot trigger another Start after reopening the journal.
Multi-step/repeated-cycle timing, startup cancellation, ambiguous-failure recovery, live optical
acquisition and calibrated well measurements remain outside the tested scope.

## Single raw snapshot from Ubuntu

Session `abi7500-plr-snapshot-001` ran on 2026-10-09 from 21:05:04.145 to 21:05:06.386 UTC,
with explicit approval for one 0.256-second snapshot. It used the same C9254-03/2.20 camera and
G2.10 controller directly through Ubuntu PLR USB, with Windows disconnected. The existing sealed
plate and lamp/filter settings were retained. No thermal run, filter/plate movement, lamp command,
recalibration or firmware change was performed.

| Observation | Result |
|---|---|
| Acquisition ID | `plr-snapshot-001`, durably reserved before exposure |
| Exposure setting | One `CJ   2,  256`; reply `00 01` confirms the 256 ms setting |
| Capture | Exactly one CC, at 21:05:05.323 UTC |
| Readiness | 27 `not r` replies followed by one `ready` |
| Image read | One `CD  64,2458 `; 157,312 bytes in 38 × 4,096-byte reads plus 1,664 bytes |
| Timing | 0.307 seconds from CC to CD; image transfer took 0.357 seconds |
| Native crop | All 76,800 pixels matched isolated ArpLib image-copy execution exactly |
| Optical observations | Filter `00 00`, stationary motor `00 00`, opaque lamp reply `00 00 00 02`, unchanged |
| Thermal observations | Inactive before/after; sample/block 25.0 °C; cover 105.1→105.3 °C |
| Repeated API call | Same ID/exposure returned the saved result with zero additional USB I/O |
| Teardown | USB closed cleanly; no further instrument commands |

The raw crop spans 115–419 counts (median 129); no pixel is zero or at the uint16 maximum. These
statistics do not establish fluorescence, signal quality or optical calibration. Filter index 0
is not assigned a dye. The timing above includes polling and transport overhead and does not
independently measure sensor integration time. The cover remained hot when USB was closed.

All 13 thermal exchanges matched command/address/sequence and passed length/checksum checks with
zero firmware errors. The transcript contains 304 completed writes and 323 reads. Thermal commands
were limited to application-mode, identity, status and optical-state queries. The journal's existing
thermal run records were unchanged. No snapshot driver correction was required for the hardware run.

Full evidence remains outside Git in `abi7500-research/captures/abi7500-plr-snapshot-001/`: exact USB
traffic, source snapshots, journals, acquisition metadata, original/cropped pixels, native crop
validation, pixel statistics and a labeled preview. The archive is stored in Seafile
`Temp/ABI-7500/abi7500-plr-snapshot-001.zip`.

| Artifact | SHA-256 |
|---|---|
| `usb-hex.jsonl` | `5400f94b7bba755a714d9619048633d22c087c5e60ae3961ae593b4c820b8bd7` |
| `camera-original.bin` | `5b4cf75ab76b92fd7cff07a8c2e95d8ac2b925495d6496b775aee6fdf904e1ba` |
| `camera-image-u16le.bin` | `a7382d3665c965c2f28fe5042cacd8d4c2db9cc96f5c8e53849dd1b5b225dfa4` |

`test_data/ubuntu_snapshot_vector.json` preserves a compressed transcript of the complete snapshot
method and its original frame for offline replay. It checks actual serial fragmentation, readiness,
native optical observations, raw image fidelity and the independently emulated crop hash. Other
exposure settings, lamp/filter control, optical fault recovery, physical well mapping, synchronized
acquisition and calibrated per-well measurements remain unverified.

## Lamp/filter control and one filter-1 snapshot

Session `abi7500-plr-optics-001` ran on 2026-10-09 from 23:59:11.049 to 23:59:22.226 UTC with
explicit approval for the prepared optical session. The same instrument was controlled directly
from Ubuntu through PLR USB. The executed sources, approval, persistent-journal snapshots and
full hex transcript are archived outside Git with the capture.

| Operation | Observed result |
|---|---|
| Initial readback | Inactive, filter 0, stationary motor, lamp `0000 0002` |
| Filter selection | One absolute move `0x69/0001`, confirmed stationary at filter 1 |
| Lamp selection | One enable `0x64/0001`, then one power `0x65/0000`; readback `0001 0000` |
| Repeated setters | Queries only, no duplicate mutations |
| Acquisition | `plr-optics-001-f1`; one CJ 256, one CC and one CD |
| Readiness/transfer | 27 `not r`, then `ready`; all 157,312 bytes received |
| Native image copy | All 76,800 cropped pixels matched isolated ArpLib execution |
| Repeated acquisition ID | Cached result, zero additional USB traffic |
| Restoration | One lamp disable `0x64/0000`, one filter move `0x69/0000`; original state confirmed |
| Final status | Inactive, sample/block 25.0 °C, cover 104.6 °C; USB closed |

No thermal start/upload, temperature setpoint, plate motion, homing, recalibration or firmware
command was sent. No cleanup reconnect was needed. All 101 thermal exchanges had valid framing,
checksums, matched command/sequence/address fields and zero firmware errors. The transcript
contains 1,749 completed writes and 1,673 reads. CC-to-CD took 0.308 seconds and image transfer
took 0.356 seconds. This is a PLR transport transcript, not an independent USB analyzer capture.

The raw image ranges from 116 to 411 counts, with median 129 and mean 129.411. This is close to
the earlier lamp-off snapshot (mean 129.572); no usable fluorescence was established. The session
validates desired-state commands, readback, duplicate prevention and image transfer, **not optical
output or stabilization**. Lamp faults, other filters/power settings and recovery remain untested.
The cover remained hot. The runner's successful flag refers to its control/transfer checks.

Full evidence, original and separately corrected images, diagnostics, byte audit and source
snapshots are in `abi7500-research/captures/abi7500-plr-optics-001/`. The diagnostic well output uses
the backed-up F0 mask with an explicit filter-1 association; it is not calibrated fluorescence.
The `ubuntu_optics_vector.json` regression fixture replays the complete optical sequence and
frame with original serial fragmentation. Only between-method `C:` keepalives are omitted.
The evidence archive is saved in Seafile `Temp/ABI-7500/abi7500-plr-optics-001.zip`; a downloaded
server copy matched the local SHA-256 `160689e2d2d361f242a4e9d306150d6dae2a07586af0e8b5952a344a3ee23580`.

| Artifact | SHA-256 |
|---|---|
| `usb-hex.jsonl` | `3e9db8f657391d134e0c795b03b8b1fdfbd34d9ff63fad2e6421bdcf2da8d7b7` |
| `camera-original.bin` | `ce754a30fc4c5263f63b4515c528be62621dd8bb9fda6a0549237ff02d8a1ea5` |
| `camera-image-u16le.bin` | `e42c276a691da5f05bcd0764b6b60f023a5e367f3af7c33fb0f585c0cff87560` |

## Ubuntu illumination session

Session `abi7500-plr-illumination-001` ran on 2026-10-10 from **00:21:32.679 to 00:22:09.076 UTC**
(2026-10-09 locally), with explicit approval to resolve illumination. PLR controlled the same
C9254-03/2.20 and G2.10 instrument through USB on Ubuntu, using the existing persistent journal.
The driver applied the native 250 ms enable-to-power and post-mutation delays. The runner also
waited five seconds before each filter-1 lamp-on exposure and one second after lamp-off before
the final dark frame.

| Frame ID suffix | Physical filter | Lamp / power | Exposure | Crop mean | Crop maximum | Pixels at 4095 |
|---|---|---|---|---|---|---|
| `off-f1` | 1 | off | 256 ms | 129.470 | 412 | 0 |
| `p0-f1` | 1 | on / 0 | 256 ms | 154.658 | 433 | 0 |
| `p1-f1` | 1 | on / 1 | 256 ms | 160.091 | 440 | 0 |
| `p1-f5` | 5 | on / 1 | 90 ms | 1436.659 | 4095 | 19487 |
| `off-f5` | 5 | off | 90 ms | 129.282 | 228 | 0 |

All IDs start `plr-illumination-001-`. Lamp-on frames visibly resolve the 96-well pattern; lamp-off
frames return to the dark background. This resolves the missing optical signal for the tested
sequence. It does not isolate the contribution of the native delays versus the extra settling
interval or establish calibrated lamp intensity. Filter 5 is saturated at this exposure.

The byte audit confirms exactly five CJ/CC/CD acquisitions and these seven mutations in order:
`69/0001`, `64/0001`, `65/0000`, `65/0001`, `69/0005`, `64/0000`, `69/0000`.
Final readback is lamp `0000 0002`, stationary motor `0000`, filter `0000`. No reconnect was needed.
Repeating the last acquisition ID returned its cached result with **zero additional USB I/O**.
The journal retained prior thermal runs and records all five acquisitions complete.

No thermal upload/start/setpoint, plate motion, homing, firmware, recalibration or counter-reset
command was sent. Sample/block readings were 24.9–25.0 °C; the already heated cover remained
104.6–105.3 °C. USB closure does not cool the cover. The transcript contains 3,186 completed writes
and 3,151 reads; it records PLR transport calls rather than an independent analyzer capture.

| Original camera frame | SHA-256 |
|---|---|
| `off-f1` | `41601fa6da6f689dfd3314763f9f681e062e472632f0d8f24d00d55e2b12c3a5` |
| `p0-f1` | `f62b8c5479e9183305f220e5b8126fbee9c5b1e24e0aed5312c18375264b47f5` |
| `p1-f1` | `8a88253f2db482fc0d6388548a8c8ae881c20d273442059cf94eb85a3a17eb40` |
| `p1-f5` | `fd12595f6d96c33334f37cee58ed70083aeaff9b365625dc866d1c3ba1bfec35` |
| `off-f5` | `f77804d88fb2ae92c14ba039d5104b4dc40cf864d9475295a858ccbd06e96213` |

Full frames, metadata, the comparison image, audited USB transcript and executed-source/journal
snapshots are preserved outside Git in `abi7500-research/captures/abi7500-plr-illumination-001/`.
The USB transcript SHA-256 is
`d7fe085924a3a678b16352c9c64ebacb12fac4d0950a7fb8f9204f595051bbb3`.
Before execution, the runner passed seven offline mock scenarios: success, unexpected initial
optics, and failure at each of the five acquisitions. Cleanup never repeats an exposure.

The evidence archive is saved in Seafile `Temp/ABI-7500/abi7500-plr-illumination-001.zip`.
A downloaded server copy matched SHA-256
`f6690b92f7a7aa8208fbc87331ae3a94133149dcb6120d1b1ef5b42bcd4373d9`.
It includes the offline comparison report and source snapshots in addition to the session evidence.

## Camera processing compared with the same vendor acquisition

This separate offline comparison uses the original completed vendor run, not the five illumination
frames above. It reconstructs all 30 collection frames into 96 wells × five saved filters × two
cycles, with explicit frame/exposure associations, native masks, matching `Background.csv` and the
EDS's uniformity snapshot. **All 960 original integers match exactly.** All **960 corrected binary32
values** also match the separately vendor-validated Java uniformity operation.

The verified chain includes the OPB=0 crop, 180° rotation into mask coordinates, nonzero-pixel
sum, adjacent-ring correction, native exposure normalization, background offsets/slopes,
saturation-dependent exposure choice, reversed physical/saved filter order and uniformity.
See [protocol evidence](protocol.md) for native routine addresses and calibration hashes and
[the processing API](optics-and-measurements.md) for original/intermediate data preservation.

The version-5 native background CSV retains enough coefficient precision for exact reconstruction;
the EDS INI alternative misses ten integers. No coefficients were fitted to vendor results.
The full comparison report is `abi7500-research/captures/abi7500-acquire-001/camera-to-vendor-validation.json`.
Ten small recorded image patches and an 80-case native exposure-selection oracle provide portable
regression fixtures. Saturated fallback results are explicitly flagged. This verifies the recorded
configuration and its saved well ordering, not independent physical A1 orientation, current
calibration validity, dye unmixing, Ct, or assay accuracy. A successful synchronized PLR run remains unverified.

## First synchronized-acquisition attempt

Session `abi7500-plr-sync-001` ran on 2026-10-10 from **01:03:47.269 to 01:04:05.416 UTC**
(2026-10-09 locally). It attempted two repetitions of 35 °C/15 s then 30 °C/60 s, with collection
planned during each 30 °C hold. Lamp power 1 was enabled with five seconds of settling.
**This session did not validate synchronized acquisition: no camera exposure occurred.**

The first status/history/status observation straddled thermal startup: the first status still
belonged to the preceding run, whereas history and the second status identified the new run.
The scheduler incorrectly treated that temporary `uncertain` result as a permanent optical error.
Separately, the validation script tried to display a nonexistent serialized `last_status` field
instead of `status_hex`, masking the collector error. The script's cleanup reconciled fresh owned
active status, cancelled the same run, confirmed tagged aborted history, and requested 25 °C idle.
It then disabled the lamp; the filter was already at 0.

The byte audit establishes one prepare/upload/Start, one Stop and one idle-target write; lamp
mutations were `64/0001`, `65/0001`, `64/0000`. There were **zero CJ, CC or CD commands**, no filter
motion, no second Start, no reconnect, and no firmware/calibration writes. The persistent record
is `cancelled`, with the optical failure retained. Final sample/block/cover readings were
25.8/24.6/104.9 °C, phase inactive, lamp `0000 0002`, stationary filter `0000`. The cover remains hot.

The scheduler now waits for coherent ownership evidence after a transient uncertain observation,
without acquiring in an uncertain state. The runner now displays the actual serialized field,
reports a completed collector's exception first, and always retrieves its task result. The new
`ubuntu_sync_startup_vector.json` retains the original startup and cleanup observations. Offline
regression tests replay that transition, complete two synthetic scheduled collections, exercise
fragmented images and interrupted/cross-boundary exposures, and verify duplicate prevention.
Runner mocks now use actual `ThermalRun.to_dict()` records and exercise progress reporting.
**The subsequent session below exercised these fixes and acquired all 30 planned images.**

Original executed source, before/after journals, all USB transfers, result, and audit are retained
outside Git under `abi7500-research/captures/abi7500-plr-sync-001/`. Transcript SHA-256:
`ec159a986f464f81490b61697b3061b8e1614fc08991d5ae4c9ab1fee3fcad49`.
The separately approved session below used a new identity for the same two-cycle test; the
cancelled identity was not restarted.

## Thirty synchronized images; final hold interrupted by host timeout

Session `abi7500-plr-sync-002` ran on **2026-10-10, 11:02:37.882–11:10:28.791 UTC**, with explicit
approval for two 35 °C/30 °C cycles and 30 images. Ubuntu PLR used the existing journal and native
USB transport. The instrument performed two repetitions of 35 °C/15 s then 30 °C/60 s. Both
scheduled collections completed, each using five physical filters and three exposures per filter.
**Optical collection succeeded, but the full thermal program did not complete normally.**

| Collection | Original frames | Hold remaining before/after collection | First-frame to last-frame interval | Reported sample at frame boundaries |
|---|---|---|---|---|
| Cycle 1, group 0/step 1 | 15 | 44 → 14 s | 28.201 s | 30.0 °C throughout |
| Cycle 2, group 0/step 1 | 15 | 44 → 13 s | 27.595 s | 30.0 °C throughout |

Each original camera transfer contains 157,312 bytes. Every frame's pre-exposure and post-transfer
status identifies the same run token, group, step, repetition and holding phase; lamp/filter
readbacks agree before and after. All 30 exposure records and both collection records are
`complete`. No image was retried, and no collection crossed a thermal boundary. These are
controller-reported temperatures and hold associations, not independent temperature calibration
or hardware-trigger/subsecond synchronization measurements.

The cover began around 60 °C. Start acknowledgement preceded the first active thermal observation
by **263.245 seconds**. The runner's **450-second total observation budget included this warmup**.
It expired after both collections, before the final hold ended. Cleanup reconciled the owned run
and sent Stop with **eight reported seconds remaining** in that hold, then requested the captured
25 °C idle target. The final run checkpoint is `cancelled`, its history is tagged `plr-sync-002` /
`PLR` and `aborted`, and the timeout is retained independently of the successful optical records.
This was a host observation timeout; the byte audit found no USB transfer errors or firmware errors.

The audit confirms one prepare/upload/Start, exactly **30 CJ / 30 CC / 30 CD** commands, one Stop
and one idle-target command. Optical mutations enabled lamp power 1, moved through filters 1–5
twice, disabled the lamp, and restored filter 0. There was no homing, plate-loading movement,
reconnect, firmware/calibration write or second Start. At disconnect, status was inactive with
sample/block/cover at **29.2/27.5/105.3 °C**, lamp `0000 0002`, motor `0000`, filter `0000`.
The 25 °C setpoint had been acknowledged; the sample had not yet reached it, and the cover was hot.

Offline processing produced **960 well/filter/cycle values** from these new frames using the
explicitly associated reference-PC mask, native background and uniformity backups. Original
frames, extraction statistics, all exposure candidates, selected indices and calibration hashes
are preserved. **630 of 960 selected values are flagged saturated**; none of the filter-1 values
selected a saturated exposure. These flagged values are not quantitative assay results. The
previous exact vendor comparison is separate evidence; there is no matching vendor output for
this new PLR acquisition, and current calibration validity has not been established.

The subsequent approved session below uses a **900-second** observation budget while retaining the same
thermal program and exposure plan. Timeout messages now distinguish the overall fluorescence
observation deadline (including startup) from a USB command timeout. Regression tests verify
that an outer timeout after complete collections preserves all original frames, prevents another
read, and does not relabel thermal cancellation as success. Original frame-boundary metadata is
retained in `ubuntu_sync_vectors.json`. The separately approved repeat is reported below.

Evidence is outside Git in `abi7500-research/captures/abi7500-plr-sync-002/`, including
`well-measurements.json`, the temperature trajectory and image plots, byte audit, original frames,
and executed-source and before/after journal snapshots. USB transcript SHA-256:
`8f15785313a96cb9a43ab9f10cab5ae214282057b07b84775930cf1eb7c336ab`.

## Normally completed two-cycle optical run

Session `abi7500-plr-sync-003` ran on **2026-10-10, 11:31:45.298–11:35:21.475 UTC**, with fresh
approval for the same two-cycle, 30-image program and a **900-second observation budget**. Ubuntu
PLR controlled the instrument directly through USB with Windows disconnected, retaining the
existing journal and using a new run identity. Both repetitions of 35 °C/15 s then 30 °C/60 s
completed normally. Matching history identifies `plr-sync-003` / `PLR`, outcome `completed`,
and a 198-second thermal duration. The run and optical records have no errors.

| Collection | Original frames | Hold remaining before/after collection | First-frame to last-frame interval | Reported sample at frame boundaries |
|---|---|---|---|---|
| Cycle 1, group 0/step 1 | 15 | 44 → 14 s | 28.149 s | 30.0 °C throughout |
| Cycle 2, group 0/step 1 | 15 | 45 → 13 s | 28.124 s | 30.0 °C throughout |

All 30 original frames contain 157,312 bytes and reconstruct the captured 321 × 245 wire geometry
and 320 × 240 image crop. Pre-exposure and post-transfer status agree on the run token, holding
phase, group, step and repetition. Lamp/filter readbacks also agree. Original transfers, cropped
images and metadata are retained. Repeating `collect_fluorescence()` after completion returned
identical saved results with **zero USB operations**.

The complete byte audit found one prepare/upload/Start, exactly **30 CJ / 30 CC / 30 CD** commands,
and **no Stop or idle-target write**. Lamp power was 1; filters traversed 1–5 twice, followed by
lamp disable and restoration of filter 0. All thermal checksums, sequence IDs, reply error codes
and serial-read counts validated. No USB errors, exposure retries, homing, plate-loading movement,
reconnect, firmware/calibration writes or second Start occurred.

The cover was already **104.7 °C** at connection, and the first active thermal observation followed
Start acknowledgement by 1.057 seconds. This validates normal completion using the longer host
budget with a warm cover; it does not establish the required time for cold-cover startup or other
programs. Collection intervals are controller-observed hold associations, not independent
temperature calibration or hardware-trigger/subsecond timing measurements.

Cleanup verified inactive state 2, stationary filter `0000`, and lamp `0000 0002`. Final
sample/block/cover readings were **30.0/29.9/105.0 °C**. Normal completion retained the final 30 °C
target; no cooling command was sent and the cover remained hot.

Offline processing produced **960 well/filter/cycle values**, with extraction statistics, exposure
candidates, selections and explicitly associated calibration hashes. **578 selected values are
flagged saturated**; all 192 physical-filter-1 values selected unsaturated exposures. The saturated
values are not quantitative assay results. The reference-PC backup masks, background coefficients
and uniformity factors were applied without changing calibration. Their current validity remains
unverified. The earlier exact comparison against vendor outputs is separate evidence; this new
PLR acquisition has no same-acquisition vendor output comparison.

`ubuntu_sync_completed_vectors.json` retains small original frame-boundary and completion
observations. Offline tests replay the final status/history/status observations, verify the saved
frame associations, and ensure repeated calls cannot restart or cancel the completed run. The
experimental warning remains for broader programs, communication-fault recovery, physical well
labeling, unsaturated measurement comparisons and current calibration validity.

Full evidence is outside Git in `abi7500-research/captures/abi7500-plr-sync-003/`: executed source,
before/after journals, USB transcript, audit, original frames, calibration inputs, per-well results
and temperature/image plots. USB transcript SHA-256:
`279a48cd157e78f57027ec847ef2e0ceef7f42e8bf96b7703ddfd2ba3e1912c5`.

## Shorter exposures remove selected saturation flags

Session `abi7500-plr-lowexp-001` ran on **2026-10-10, 11:50:28.358–11:54:04.364 UTC**, under fresh
approval for one shorter-exposure validation. Ubuntu PLR used the same sealed plate, persistent
journal, two-cycle 35 °C/15 s then 30 °C/60 s program, lamp power 1 and 900-second observation
budget. Filter 1 retained 540/180/60 ms; filters 2–5 used **30/10/5 ms**. Both cycles completed
normally, with matching tagged history reporting a 196-second thermal duration.

| Collection | Frames | Hold remaining before/after collection | First-frame to last-frame interval | Reported sample at every frame boundary |
|---|---|---|---|---|
| Cycle 1, group 0/step 1 | 15 | 45 → 18 s | 24.288 s | 30.0 °C |
| Cycle 2, group 0/step 1 | 15 | 44 → 16 s | 24.584 s | 30.0 °C |

Every original image contains 157,312 bytes; all run/hold/filter associations and exposure echoes
validated. Repeated retrieval after completion returned identical saved collections with no USB
I/O. The complete transcript audit confirms one prepare/upload/Start, **30 CJ / 30 CC / 30 CD**,
zero Stop/idle-target commands and no USB or firmware errors. There was no exposure retry, second
Start, homing, plate-loading motion, reconnect, firmware write or calibration change.

All **960 selected well/filter/cycle values were unflagged**, compared with 578 flagged values in
`plr-sync-003`. This uses the recovered vendor rule: an exposure is saturated when more than six
selected well pixels exceed 4000 counts. It is not a claim that every pixel in every frame is
unclipped. The selection algorithm retained the longer exposures where usable and chose shorter
ones where necessary:

| Physical filter | Saturated selections before / 192 | Saturated selections after / 192 | Selected exposure counts across both cycles |
|---|---|---|---|
| 1 | 0 | 0 | 192 at 540 ms |
| 2 | 170 | 0 | 138 at 30 ms; 54 at 10 ms |
| 3 | 117 | 0 | 192 at 30 ms |
| 4 | 111 | 0 | 192 at 30 ms |
| 5 | 180 | 0 | 16 at 30 ms; 168 at 10 ms; 8 at 5 ms |

At filter 2, 54 well/cycle extractions were flagged at 30 ms. At filter 5, 176 were flagged at
30 ms and eight at 10 ms. All 5 ms extractions were unflagged. Original frames and every exposure
candidate remain preserved, including the saturated candidates that were not selected.

**Quantitative stability remains unresolved.** Comparing the same fixed, unsaturated 5 ms
exposures between cycles gives median cycle-2/cycle-1 ratios of 0.992, 0.987, **0.898** and
**0.923** for physical filters 2, 3, 4 and 5 respectively (96 wells each). The roughly 10% and 8%
declines at filters 4 and 5 also appear at other unsaturated exposures. They therefore are not
explained solely by switching the selected exposure. This session does not identify their cause.
The unchanged filter-1 settings gave a median current/previous-run ratio of 0.982 over 192
well/cycle comparisons.

Among unsaturated, positive candidates, exposure-normalized 5 ms signals were typically about
2.0–2.3% below corresponding 10 ms signals at filters 2–5. These comparisons use pre-uniformity
vendor-compatible integers, exclude saturated or nonpositive candidates and preserve the sample
counts in `exposure-comparison.json`. They are descriptive checks, with no prespecified precision
acceptance threshold. They do not establish linearity, current calibration validity or assay accuracy.

The cover began at 104.6 °C; the first active observation followed Start acknowledgement by
1.530 seconds. Cleanup verified inactive state 2, lamp `0000 0002`, stationary filter `0000` and
sample/block/cover readings of **30.0/30.0/104.9 °C**. No cooling command was sent. Cold-cover
startup with this budget, broader programs and communication-fault recovery remain unverified.

The exact reference-PC masks, background coefficients and uniformity factors were reused and
identified by hashes. There is no same-acquisition vendor output for this PLR run. Independent
physical well orientation and quantitative consistency still need validation; no Ct, dye
unmixing, assay interpretation or recalibration was performed.

`ubuntu_short_exposure_vectors.json` preserves six original command/reply pairs and counts for
offline regression testing. Full evidence remains outside Git in
`abi7500-research/captures/abi7500-plr-lowexp-001/`, including executed source, before/after
journals, original images, calibration inputs, per-well results, the exposure comparison and
plots. USB transcript SHA-256:
`215febf6a20292f7bf11427fd1aee8a4947820a20b6dab7c2ababdfec65db844`.

(abi7500-offline-cycle-drift-audit)=
## Offline cycle-drift audit

Filter numbers in this audit are **physical wheel positions**, not vendor channel numbers.
Physical position 5 maps to vendor channel 1 (SYBR/FAM), and position 4 to vendor channel 2
(VIC/JOE). See the [channel mapping](optics-and-measurements.md). The plate was subsequently
reported by the user to contain SYBR Green; the decline at physical position 5 therefore
includes its principal detection channel, not the vendor's Cy5 channel.

`abi7500-drift-001` compared **120 original frames** from the vendor acquisition,
`plr-sync-002`, `plr-sync-003` and `plr-lowexp-001`, without hardware I/O. An independent NumPy
implementation copied the original wire words, applied the recovered crop/rotation, and summed
the saved masks and surrounding rings. All 90 PLR crops matched their saved image bytes;
**8,640 well/exposure extractions and background/exposure-corrected candidates matched** the
saved PLR records. Reconstructed exposure selection also matched all **960 original vendor EDS
integers**. The audit reused the hashed calibration inputs without changing measurements or
adding a drift correction.

The stricter comparison below uses the same exposure and well in both cycles, requires positive
signals, and excludes any well with even one selected pixel above 4000 in either frame. This is
stricter than the native saturation flag, which permits up to six such pixels. Values are median
cycle-2/cycle-1 ratios; the different stages use the same 96 wells in the short-exposure session.

| Physical filter / exposure | Uncorrected pixel sum | Ring-subtracted signal | Background/exposure-corrected candidate |
|---|---|---|---|
| 1 / 60 ms | 0.9987 | 0.9928 | 0.9924 |
| 2 / 5 ms | 0.9937 | 0.9917 | 0.9917 |
| 3 / 5 ms | 0.9934 | 0.9869 | 0.9869 |
| 4 / 5 ms | 0.9503 | 0.8977 | 0.8978 |
| 5 / 5 ms | 0.9352 | 0.9226 | 0.9228 |

The decrease is already present in uncorrected pixel sums. Subtracting surrounding pixels
changes the relative decrease, but subsequent calibration/exposure normalization changes the
filter-4/5 ratios only slightly. It persists at other unsaturated exposures, so neither exposure
selection nor calibration processing alone explains it. At filter 1, the longer 540 ms exposure
has a ring-subtracted median ratio of 0.9883 and less well-to-well scatter than 60 ms.

The **vendor-controlled acquisition also shows declines**: at its shortest exposures, the
ring-subtracted ratios are 0.9056 at filter 4/85 ms (**10 matched wells**) and 0.9361 at filter
5/30 ms (**3 matched wells**). Longer vendor exposures clipped most wells, limiting this
comparison. Its timing and acquisition settings differ; this is evidence that a similar decline
is not exclusive to PLR, not proof of an identical cause or a controlled software comparison.

Across the short-exposure session's 15 matched frame pairs, optical-black reference means changed
by less than 0.6 camera counts. Whole-image translation estimates were at most 0.02 pixel in
either axis; this descriptive estimator is not a physical-motion bound and does not exclude
other optical changes. Frame-boundary sample readings were 30.0 °C and recorded lamp/filter
states matched the requested settings. These observations do not independently measure sample
temperature, lamp output, filter transmission or fluorescence stability.

This is a two-collection comparison on one existing plate. The wells are not independent run
replicates, and no precision acceptance threshold was prespecified. Plate changes, illumination
changes and thermal history remain possible explanations. Current calibration validity and
physical well labeling remain unverified. No automatic normalization or runtime driver change
is justified by this audit.

The diagnostic comparison used two repetitions of 30 °C/40 s then 30 °C/60 s, collecting
15 frames in each second hold, about 100 seconds apart. It preserved the short-exposure settings,
lamp power and filter order while removing the intervening 35 °C excursions. The runner passed
nine offline mock scenarios before the separately approved hardware session described below.

Analysis code, input hashes, per-exposure statistics and plots remain outside Git in
`abi7500-research/analyses/abi7500-drift-001/`. The prepared session is
`abi7500-research/plans/abi7500-plr-iso30-001/`.

(abi7500-constant30-validation)=
## Constant-30 °C optical comparison

Session `abi7500-plr-iso30-001` ran on **2026-10-10, 12:14:36.154–12:18:27.760 UTC**, with fresh
approval for one constant-30 °C session. Ubuntu PLR used the existing sealed plate, reported by
the user to contain SYBR Green, and the persistent run journal. Every uploaded sample target
was 30 °C: two repetitions of 40 s then 60 s, with collection during each second hold. The
200 seconds of programmed holds completed normally; matching tagged history reported a
212-second thermal duration. The observation budget was 900 seconds, including startup.

Physical position 1 used 540/180/60 ms; positions 2–5 used 30/10/5 ms. Lamp power was 1, with
five seconds of settling and the same filter order as `plr-lowexp-001`.

| Collection | Original frames | Hold remaining before/after collection | First-frame to last-frame interval | Reported sample at frame boundaries |
|---|---|---|---|---|
| 1, group 0/step 1 | 15 | 44 → 18 s | 24.178 s | 30.0 °C throughout |
| 2, group 0/step 1 | 15 | 45 → 16 s | 25.114 s | 30.0 °C throughout |

First exposures were **103.098 seconds apart**, compared with 98.672 seconds in the earlier
short-exposure 35 °C/30 °C run. All 30 original frames contain 157,312 bytes; saved crops,
exposure echoes, run identities, hold associations and stationary optical readbacks agree.
The complete USB audit validated framing/checksums, sequence IDs and replies: one
prepare/upload/Start, exactly **30 CJ / 30 CC / 30 CD**, and no Stop or idle-target write.
There was no USB error, camera retry, homing, plate-loading movement, reconnect, firmware or
calibration write. Repeated collection retrieval returned the saved results with zero USB I/O.

All **960 selected well/filter/collection measurements were unflagged** under the native
saturation rule. Some unused longer exposures still clipped wells. The table below instead
requires **zero selected pixels above 4000** in all four matched observations: both earlier
cycles and both constant-temperature collections. Every row has 96 positive matched wells.
Ratios are medians of collection-2/collection-1 background/exposure-corrected candidates before
uniformity, at one fixed exposure. Dye names identify channel assignments, not unmixed dye signals.

| Vendor channel | Physical position | Exposure | Earlier 35 °C/30 °C ratio | Constant-30 °C ratio |
|---|---|---|---|---|
| 1 — SYBR/FAM | 5 | 5 ms | 0.9228 | **0.9394** |
| 2 — VIC/JOE | 4 | 5 ms | 0.8978 | **0.9255** |
| 3 — NED/TAMRA/Cy3 | 3 | 5 ms | 0.9869 | 0.9908 |
| 4 — ROX/Texas Red | 2 | 5 ms | 0.9917 | 0.9935 |
| 5 — Cy5 | 1 | 540 ms | 0.9877 | 0.9894 |

The decline persisted without intervening 35 °C excursions: about **6.1% in the SYBR/FAM
channel** and **7.5% in vendor channel 2**. It was already present in uncorrected pixel sums;
ring-subtracted ratios were 0.9392 and 0.9254, respectively. An independent NumPy extraction
matched all 5,760 saved well/exposure extractions and candidates across this and the comparison
session. Calibration inputs matched by hash. No drift normalization or runtime driver change
was made.

This establishes that the 35 °C excursions are not required for the observed decline. It does
not identify its cause or establish that removing those excursions caused the smaller decline:
each condition has only one run on the same aged plate, at different times and with different
prior illumination and thermal history. Wells are not independent run replicates. Dye/plate
changes, illumination changes and other time-dependent effects remain possible. Current
calibration validity, physical well labeling and quantitative assay accuracy remain unverified;
no dye unmixing, ROX normalization or Ct analysis was performed.

Initial sample/block/cover readings were 25.0/25.0/105.4 °C. The first active observation followed
Start acknowledgement by 1.018 seconds. All programmed targets were 30 °C; the initial approach
to that target is retained in the temperature trace. Cleanup confirmed inactive state 2,
lamp `0000 0002`, stationary filter `0000`, and final readings of **30.0/29.9/104.6 °C**.
No cooling command was sent; the cover remained hot.

Full evidence remains outside Git in `abi7500-research/captures/abi7500-plr-iso30-001/`: executed
source, before/after journals, original frames, calibration inputs, complete USB transcript,
audits, per-well results, matched comparisons and plots labeled with both numbering systems.
USB transcript SHA-256:
`de842bf2dffbc305912147538443d38ac128dd6bf71d7a95a7367dcd6d9f66fb`.

(abi7500-parked-validation)=
## Parking between collections with the lamp enabled

Session `abi7500-plr-park-001` ran on **2026-10-10, 13:05:55.402–13:10:17.308 UTC**, under
explicit approval to verify and close the illumination path between collections while keeping
the lamp warm. It used the existing sealed SYBR Green plate and the same constant-30 °C thermal
program and 30-image exposure plan as `plr-iso30-001`. The added setting was
`CollectionPlan(..., park_between_collections=True)`. It commands absolute position 0 after
both collections, waits for stationary readback, verifies unchanged lamp power 1, and persists
that evidence before recording a complete collection. The same hold/run identity must still
match after parking. No home command or calibration write is sent by this option.

### Optical-path check

Before the thermal Start, eight idle diagnostic frames compared lamp-off/filter 5,
lamp-on/position 0, lamp-on/filter 5, then lamp-on/position 0 again, at 5 and 30 ms each.
The reported sample temperature was 25.0 °C for these checks. Lamp power 1 settled for five
seconds before the first parked read; subsequent diagnostics added warmup before the thermal
run. At **unsaturated 5 ms**, full-image mean counts were:

| State | Mean camera count |
|---|---|
| Lamp off, physical filter 5 | 129.296 |
| Lamp on, position 0 before open read | 134.069 |
| Lamp on, physical filter 5 | 395.737 |
| Lamp on, position 0 after open read | 134.050 |

After subtracting the lamp-off baseline, position 0 retained approximately **1.8% of the
open-path signal**, or **98.2% suppression**. A small exposure-dependent residual remained.
Both parked observations passed the prepared gate (open-minus-dark mean above 10 counts;
parked-minus-dark no more than the larger of 3 counts or 5% of open-minus-dark). The 30 ms open
image clipped 15,073 pixels above 4000, so it is not used to quantify linear attenuation.
Camera suppression supports the optical-path interpretation; it does **not** directly measure
excitation irradiance at the sample or prove complete darkness at the wells.

### Synchronized acquisition and comparison

The thermal program completed normally with matching tagged history reporting **212 seconds**.
There was one prepare/upload/Start, **38 CJ/CC/CD** operations (eight diagnostics plus 30
scheduled images), no retry, Stop, idle-target write, homing, reconnect or plate movement.
Lamp enable/power stayed at 1 throughout the two collections and their intervening interval.
The USB audit validated exact framing, checksums, sequence IDs, counted reads and allowed commands.

| Collection | Scheduled frames | Hold remaining before / after parking | First to last frame | Sample at frame boundaries |
|---|---|---|---|---|
| 1 | 15 | 44 / 13 s | 25.322 s | 30.0 °C |
| 2 | 15 | 45 / 13 s | 25.361 s | 30.0 °C |

First exposures were **101.669 seconds apart**. Both collection records contain stationary
position 0 and lamp `0001 0001` readback. All **960 selected well measurements were unflagged**;
unused longer exposures can still clip. Cached retrieval returned identical results with no USB I/O.

The comparison below uses a fixed exposure and the same 96 positive wells in all four images
(two from each session), with **zero well pixels above 4000**. Values are median
collection-2/collection-1 ratios after background/exposure correction, before uniformity.
Channel names identify optical assignments, not spectrally unmixed dye quantities.

| Vendor channel | Physical position | Exposure | Earlier unparked 30 °C | Parked 30 °C |
|---|---|---|---|---|
| 1 — SYBR/FAM | 5 | 5 ms | 0.9394 | **0.9921** |
| 2 — VIC/JOE | 4 | 5 ms | 0.9255 | **0.9905** |
| 3 — NED/TAMRA/Cy3 | 3 | 5 ms | 0.9908 | 0.9963 |
| 4 — ROX/Texas Red | 2 | 5 ms | 0.9935 | 0.9961 |
| 5 — Cy5 | 1 | 540 ms | 0.9894 | 0.9938 |

The SYBR-channel decline was approximately **0.8% instead of 6.1%**, and vendor channel 2
approximately **0.9% instead of 7.5%**. Ring-subtracted ratios before calibration were 0.99208
and 0.99050, respectively. Independent original-word/NumPy processing matched all **5,760**
well/exposure extractions and candidates across both sessions. Calibration hashes matched;
no drift correction was applied.

This supports a contribution from inter-collection optical state, but does not prove
photobleaching. Each condition has only one run on the same aged plate; prior illumination,
time and extra diagnostic warmup differ. Wells are not independent run replicates. The
vendor sends **`0x67` home after each burst**, verified in both the native collection-end idle
path and original capture frames 11213/15211. Earlier timing comparisons that counted only
absolute `0x69` moves overlooked those home commands. The parked session tested an absolute
move to 0, not homing; a possible alignment benefit from resetting the wheel remains untested.
Current calibration validity, physical well labeling and assay accuracy remain unverified.

Cleanup confirmed inactive state 2, lamp `0000 0002`, stationary filter 0, and sample/block/cover
**30.0/29.9/104.6 °C**, then closed USB. No cooling command was sent; the cover remained hot.
Full evidence stays outside Git in `abi7500-research/captures/abi7500-plr-park-001/`, including
38 original frames, executed source, journals, calibration inputs, diagnostics, per-well
comparisons, command audit and plots. USB transcript SHA-256:
`4c416d40c41caa29e56f458ff6421fc1a6595cbc0bf51fad01b28d3eb9cfc09c`.

(abi7500-homing-validation)=
## Native homing versus absolute parking

Session `abi7500-plr-home-001` ran on **2026-10-10, 13:28:20.961–13:35:58.309 UTC**, with explicit
approval for one four-collection, 60-image comparison. It used the existing sealed plate,
persistent journal, lamp power 1 and a common **30-second warmup at position 0** before Start.
The thermal program repeated 30 °C/40 s then 30 °C/60 s four times. Collection began in each
second hold with the same short exposures: physical position 1 at 540/180/60 ms; positions 2–5
at 30/10/5 ms. `park_between_collections=True, home_after_collections=(2, 4)` requested end
operations **park → home → park → home**, without extra diagnostic images.

### Control and timing

The 400 seconds of programmed holds completed normally, with matching tagged history reporting
**416 seconds**. The first active observation followed Start acknowledgement by 1.002 seconds.
All 60 frames had a reported sample temperature of **30.0 °C** immediately before exposure and
after transfer. Both native homes returned stationary position 0 and unchanged lamp `0001 0001`.
The homing operation is private to an explicitly reserved collection; it is not an untracked
public reset command. Plan ordinals and `homing_requested` are persisted before movement.

| Collection | End operation | Hold remaining before collection / after closure | First to last frame | Command dispatch to first stationary observation |
|---|---|---|---|---|
| 1 | Absolute park (`0x69`) | 45 / 12 s | 26.121 s | 3.471 s |
| 2 | Native home (`0x67`) | 45 / 9 s | 26.288 s | 5.955 s |
| 3 | Absolute park (`0x69`) | 44 / 11 s | 26.826 s | 3.591 s |
| 4 | Native home (`0x67`) | 45 / 9 s | 26.757 s | 6.000 s |

The home waits observed 33 and 34 busy motor replies before stationary readback. Dispatch-to-readback
intervals include USB/polling latency; they are not exact mechanical motion durations. Each
operation fit within its hold. Lamp commands remained unchanged between collections. The full
USB audit verified framing/checksums, sequence IDs, transfer counts and the allowed command list:
one prepare/upload/Start, **60 CJ/CC/CD**, exactly two home commands, and no Stop, idle-target,
plate movement, firmware/calibration/counter write, reconnect or command retry. Cached collection
retrieval returned identical saved results without USB I/O.

### Signal and image comparison

All **1,920 selected well/channel/collection measurements were unflagged** by the native saturation
rule. Unused longer exposures can still clip. The table compares the same **96 positive wells
across all four collections**, requiring zero well pixels above 4000 at the fixed exposure.
Values are median next-collection/preceding-collection ratios after background/exposure correction,
before uniformity. Channel labels do not identify unmixed dye quantities.

| Vendor channel | Physical position | Exposure | 1 → 2, after park | 2 → 3, after home | 3 → 4, after park |
|---|---|---|---|---|---|
| 1 — SYBR/FAM | 5 | 5 ms | **0.99182** | **0.99529** | **0.99574** |
| 2 — VIC/JOE | 4 | 5 ms | 0.99005 | 0.99327 | 0.99478 |
| 3 — NED/TAMRA/Cy3 | 3 | 5 ms | 0.99689 | 0.99937 | 1.00093 |
| 4 — ROX/Texas Red | 2 | 5 ms | 0.99660 | 0.99897 | 0.99973 |
| 5 — Cy5 | 1 | 540 ms | 0.99363 | 0.99596 | 0.99779 |

SYBR-channel declines were **0.82%, 0.47% and 0.43%**, respectively; vendor channel 2 declines
were 0.99%, 0.67% and 0.52%. The first-exposure intervals were 102.044, 102.729 and 101.327 seconds.
The operations are assigned to the interval *after* their collection, not to the images taken
before the operation. The final home validates control/readback only: it has no subsequent image.

Independent original-word cropping and NumPy ROI/ring extraction reproduced all **5,760 unique
well/exposure extractions and candidates**. Ring-subtracted SYBR ratios before calibration were
0.99181, 0.99528 and 0.99573. Fourier whole-image registration estimated at most 0.02 pixel per
axis across the first parked interval, 0.02 after home and 0.01 after the final parked interval,
considering all 15 matched exposure pairs per interval. These are descriptive registration
estimates, not physical alignment bounds, and do not exclude optical changes other than translation.

This validates native collection-end homing on the reference C9254-03/G2.10 instrument within
this tested timing budget. It does **not** demonstrate an additional alignment or stability
benefit over absolute parking: the last parked interval was similar to the home interval and
might retain a home-induced alignment change. There is only one fixed-order run on an aged
plate, with one imaged home interval and two parked intervals. Lamp warmup, plate history and
time remain potential contributors; wells are not independent run replicates. No drift correction
or automatic switch to homing was added. Homing remains explicit and older plans keep their
original behavior. Current calibration validity, physical well labeling, photobleaching mechanism,
broader thermal programs and quantitative assay accuracy remain unverified.

Cleanup confirmed inactive state 2, stationary filter 0, lamp `0000 0002`, and sample/block/cover
**30.0/30.0/105.2 °C**, then closed USB. The cover remained hot; no cooling command was sent.
Full evidence is outside Git in `abi7500-research/captures/abi7500-plr-home-001/`: original frames,
source, journals, calibration inputs, complete USB trace, movement timings, well measurements,
independent comparisons and plots. USB transcript SHA-256:
`9e4485c61085740326765abc59d074db2f6e3d2de45d10768fb084c56749a690`.


(abi7500-cycling-home-validation)=
## Two thermal cycles with collection-end homing

Session `abi7500-plr-cychome-001` ran on **2026-10-10, 14:00:31.851–14:04:28.959 UTC**,
with fresh approval for one next-step session. It combined the previously checked thermal
program and short exposures with native homing after **both** collections and the scheduler's
checkpoint-only progress reads. The existing sealed plate was retained.

The program repeated **35 °C/15 s then 30 °C/60 s twice**, with collection in each 30 °C hold.
Physical filter 1 used 540/180/60 ms; filters 2–5 used 30/10/5 ms. The thermal upload and exposure
choices match `plr-lowexp-001`. This session used lamp power 1 and a **30-second warmup at
position 0**, compared with five seconds in that earlier session. The plan explicitly set
`park_between_collections=True, home_after_collections=(1, 2)`, with a 45-second read window
and 900-second overall observation budget. There were **30 scheduled images and no diagnostic
exposures**. Thermal timing remained on the instrument.

### Completion, timing and saved progress

The 150 seconds of programmed holds completed normally; matching tagged run history reported
**197 seconds** including the thermal transitions. The first active observation followed Start
acknowledgement by **1.024 seconds**. Every frame's before/after status identified
the intended run and hold, and reported sample **30.0 °C**. Both homes returned stationary
position 0 with lamp `0001 0001` before the collection was accepted.

| Collection | Hold remaining before collection / after closure | First to last frame | Home dispatch to first stationary observation |
|---|---|---|---|
| 1 | 44 / 8 s | 27.110 s | 6.154 s |
| 2 | 44 / 7 s | 27.763 s | 5.947 s |

The observed home intervals include USB and polling latency. Motor polling observed
34 and 33 busy replies, respectively. Both operations fit inside their holds; the second home
has no following acquisition and validates motion/readback only. First exposures were
**96.202 seconds** apart.

The full transcript audit verified packet framing/checksums, sequence IDs and the permitted
command list: one prepare/upload/Start, **30 CJ/CC/CD**, ten absolute filter selections and two
native homes. There was no Stop, idle-target write, plate-loading movement, reconnect, retry,
firmware change or calibration/counter write. Lamp commands remained unchanged between reads.
Saved progress returned the same checkpoints as the collection results, and both the explicit
progress read and repeated completed-result retrieval caused **zero USB I/O**. Progress polling
uses checkpoints without reloading accumulated image payloads; this run does not establish a
worst-case timing bound for longer programs.

### Fluorescence comparison

All **960 selected well/channel/cycle measurements were unflagged** by the native saturation
rule. Some unused longer exposures still clip. Independent cropping and NumPy ROI/ring
processing reproduced all **2,880 current well/exposure extractions and candidates**; with the
two earlier comparison sessions, 8,640 such comparisons matched.

The table uses the same **96 positive, unclipped wells in all six observations** and fixed
exposures. Ratios are median cycle-2/cycle-1 background/exposure-corrected candidates before
uniformity. Channel labels do not identify unmixed dye quantities.

| Vendor channel | Physical filter | Fixed exposure | Earlier 35/30 °C, last filter selected | Earlier constant 30 °C, absolute park | This 35/30 °C run, native home |
|---|---|---|---|---|---|
| 1 — SYBR/FAM | 5 | 5 ms | 0.92276 | 0.99210 | 0.99054 |
| 2 — VIC/JOE | 4 | 5 ms | 0.89784 | 0.99055 | 0.98877 |
| 3 — NED/TAMRA/Cy3 | 3 | 5 ms | 0.98692 | 0.99629 | 0.99693 |
| 4 — ROX/Texas Red | 2 | 5 ms | 0.99172 | 0.99615 | 0.99580 |
| 5 — Cy5 | 1 | 540 ms | 0.98769 | 0.99376 | 0.99368 |

SYBR-channel decline was **0.95%**, versus **7.72%** in the earlier cycling session and 0.79%
in the constant-30 °C parking session. Channel 2 declined 1.12%, versus 10.22% and 0.95%.
The current SYBR ratio before calibration (ring-subtracted pixels) was 0.99053, so calibration
processing does not create this difference. Whole-image Fourier registration estimated shifts
of at most 0.01 pixel per axis across all 15 matched exposure pairs; these are descriptive
estimates, not physical alignment bounds.

This validates the combined two-cycle acquisition, native homing and updated scheduler on the
reference instrument. The smaller signal decline is consistent with the earlier closed-path
results, but does not isolate a mechanism or show homing superior to absolute parking. These
are chronological sessions on one aged plate, with different prior illumination and lamp
warmup; the constant-temperature comparison also differs in thermal timing. There is only one
between-collection interval in the current run. Wells are not independent run replicates, and
plotted well percentiles are not confidence intervals. Photobleaching, current calibration
validity, physical well labeling, longer/hotter programs and quantitative assay accuracy remain
unverified. No drift normalization or automatic change to collection defaults was introduced.

Cleanup confirmed inactive state 2, stationary filter 0, lamp `0000 0002`, and sample/block/cover
**30.0/30.0/105.3 °C**, then closed USB. The cover remained hot; no cooling command was sent.
The 159-test ABI package, scoped typing and lint/format checks covered the unchanged driver
source before this run; six offline runner scenarios checked success and failure cleanup.
Full evidence remains outside Git in `abi7500-research/captures/abi7500-plr-cychome-001/`,
including source snapshots, journals, calibration inputs, original frames, USB trace,
measurements and comparisons. Transcript SHA-256:
`5c4be1060a0e9f619137e09dc4441d60b1033eeb4b064538cbe7c17af3f5baf3`.


(abi7500-sybr10-validation)=
## Ten-cycle SYBR-channel acquisition and portable export

The approved `abi7500-plr-sybr10-001` session ran **2026-10-10, 14:25:23.872–14:42:10.185 UTC**
on the reference C9254-03/G2.10 instrument from Ubuntu, with the existing sealed plate.
It repeated **35 °C/15 s then 30 °C/60 s ten times**, for 750 seconds of programmed holds.
Each 30 °C hold collected **30/10/5 ms** at physical filter **5** (vendor channel 1, SYBR/FAM),
giving **30 images and 960 selected well/cycle measurements**. Other channels were not collected.
The plan used a 45-second read window, native home after all ten collections, lamp power 1,
30-second lamp warmup at position 0 and an **1800-second overall observation budget**.
This was an engineering time series, not an amplification experiment.

### Control, timing and original data

The program completed normally with matching tagged history of **964 seconds**.
The first active observation followed Start acknowledgement by 1.030 seconds.
Every frame was associated with the intended run and hold; all before/after sample estimates
were **30.0 °C**. Every home verified stationary position 0 with the lamp
still enabled. Home dispatch to first stationary observation ranged from **5.970 to
6.158 seconds**, including polling latency.

| Cycle | Hold remaining before / after collection and home | First frame to last frame | Median 5 ms signal / cycle 1 |
|---|---|---|---|
| 1 | 45 / 30 s | 3.869 s | 1.00000 |
| 2 | 45 / 30 s | 3.696 s | 0.98952 |
| 3 | 44 / 30 s | 3.864 s | 0.98473 |
| 4 | 45 / 30 s | 3.578 s | 0.98188 |
| 5 | 45 / 30 s | 3.760 s | 0.97912 |
| 6 | 45 / 30 s | 3.813 s | 0.97639 |
| 7 | 44 / 29 s | 3.807 s | 0.97430 |
| 8 | 44 / 29 s | 3.858 s | 0.97142 |
| 9 | 44 / 29 s | 3.930 s | 0.96921 |
| 10 | 44 / 29 s | 3.759 s | 0.96691 |

The byte audit verified one prepare/upload/Start, 30 CJ/CC/CD sequences, ten physical-filter-5
selections and ten native homes. No Stop, idle-target write, reconnect or acquisition replay
occurred. Progress reads and cached completed-result retrieval added no USB traffic.
This validates ten repeats on this host within this tested budget; it is not a worst-case timing
bound or a validation of longer, hotter or multi-channel programs.

### Measurements and export

**0 of 960 selected results** were flagged by the native saturation
rule. Unused exposure candidates, including any clipped pixels, remain available. Independent
NumPy cropping and ROI/ring calculations reproduced all **2,880 well/exposure extractions and
normalized candidates**; independent exposure selection and binary32 uniformity multiplication
matched all **960 selected results**.

The fixed-5-ms trend used the same **96 positive wells with zero well pixels above 4000
in every cycle**. The median cycle-10/cycle-1 ratio was **0.96691** after background
and exposure correction (-3.31% change), with well 5th–95th percentiles
0.95993–0.97878. The corresponding ring-subtracted ratio was
**0.96690**. These percentiles describe wells, not confidence
intervals or independent run replicates. Whole-image registration estimated at most
0.02 pixel per axis relative to cycle 1; this is not
a physical alignment bound.

The three-image bursts change illumination duration and timing compared with earlier
fifteen-image collections. One chronological series on an aged plate cannot isolate
photobleaching, lamp drift or other mechanisms. No correction was applied to remove temporal
drift. Calibration validity, physical well labeling and quantitative assay accuracy remain
unverified.

After cleanup, `export_run()` produced a portable ZIP. Its 30 original images matched byte for
byte, and all 960 selected values plus 2,880 exposure candidates matched the saved evidence.
Export issued **zero USB calls** and left the journal unchanged. Original images, calibration
bytes, saturated candidates, metadata and content hashes are retained.

Cleanup confirmed inactive state 2, stationary filter 0 and lamp `0000 0002`;
sample/block/cover were **30.0/29.9/105.2 °C**.
The cover remained hot; closing USB did not cool it. The executed source matched the version
with 166 passing package tests; six offline runner scenarios covered setup rejection and failure
cleanup. Full evidence remains outside Git in `abi7500-research/captures/abi7500-plr-sybr10-001/`.
USB transcript SHA-256: `6a2691b932dfd162a039d579887705a87b205a02e6fb1aa9411b3dcf6243b11f`.

(abi7500-wellmap-validation)=
## Physical well labels with a known-pattern fluorescein plate

The approved `abi7500-plr-wellmap-001` session ran **2026-10-10,
15:36:49.610–15:38:51.489 UTC** on the reference instrument from Ubuntu.
The operator loaded a VWR **89218-296** 96-well plate with a clear cover and confirmed
**A1 at the back-left**, viewed from the front of the instrument. Wells **A1, A4, C9, F3,
H7 and H12** each contained **20 µL of 3.2 µM fluorescein**; the other 90 wells were empty.
The physical labels and orientation are operator-provided ground truth. The diluent and exact
seal product were not reported; empty wells are not solvent-matched blanks.

PLR executed one **30 °C/60-second hold**, collecting **5, 10 and 30 ms**, in that order,
at physical filter **5** (vendor channel 1). Lamp power was 1, with 30 seconds of warmup
at position 0, a 45-second read window and one native home after the burst.
Thermal completion was normal, with matching tagged history of **68 seconds**.
The collection and home began with 44 seconds remaining and finished with 29 seconds remaining.
The three-frame interval was **3.735 seconds**; every before/after sample estimate was **30.0 °C**.

### Label and image processing checks

**All six expected wells were the six brightest regions in every image**, both by raw ROI mean
and by ring-subtracted sum. No rotation, mask, labeling or calibration adjustment was made to
obtain the match. Independent NumPy reconstruction of the original camera payloads reproduced
the PLR crop; independent ROI and ring calculations matched all **288 well/exposure extractions**.

| Exposure | Positive-well raw mean range | Empty-well raw mean range | Native saturated wells |
|---|---|---|---|
| 5 ms | 350.43–545.79 | 129.28–134.64 | None |
| 10 ms | 573.81–969.61 | 130.29–139.82 | None |
| 30 ms | 1448.19–2403.40 | 133.34–157.86 | A4, C9, F3, H7 |

The 5 ms and 10 ms images had no clipped well pixels. At 30 ms the four flagged wells also
contained clipped pixels. All candidates and flags were retained; exposure selection produced
**96 results with no selected saturation flags**, using shorter images where required.
These raw pixel statistics establish signal separation, not a concentration calibration.

The six-marker pattern distinguishes the expected mapping from row reversal, column reversal,
180° reversal and column-major labeling. The expected mapping matched all six markers;
those alternatives matched 0, 0, 2 and 2 respectively. This validates the physical orientation
and **these six labels in filter 5 on this plate**. It does not exclude every arbitrary
permutation of all 96 wells or establish other channels, current calibration validity,
spectral unmixing or quantitative assay accuracy.

### Cleanup and preserved evidence

The byte audit verified one load, one prepare/upload/Start, three CJ/CC/CD sequences,
one filter selection and one native home. There was no Stop, idle-temperature write, reconnect
or exposure replay. After normal completion and matching run-history reconciliation,
cleanup disabled the lamp and verified stationary filter 0. A single journaled `0x72`
plate-release command was acknowledged; `0x73` polling confirmed state 2 → 1 → 0.
USB was then closed. The last sample/block/cover readings were **30.0/29.8/105.0 °C**;
the cover remained hot. Plate release in this session used the audited research runner;
`stop()` still only closes communication and does not release or cool the instrument.

The preceding separately approved `abi7500-plr-release-001` session used the same release command
after reconciling the completed ten-cycle run. The operator confirmed that the drawer opened
before loading this known-pattern plate. No firmware or calibration changes were made.

Offline `export_run()` retained all three original camera transfers byte for byte, all 96 selected
values and all 288 candidates, with zero USB operations and an unchanged journal. The portable
archive is `plr-wellmap-001-results.zip`, SHA-256
`f517de3dc9dfdd163e0833c03bd54caaea5c26d14ec2832eee5b45d73816f7cc`.
The unchanged driver passed **166 package tests** before this session; eight offline runner
scenarios checked setup rejection, success and failure cleanup, including ambiguous release.
Full frames, calibration inputs, USB trace, source snapshots, operator confirmation, independent
analysis and the plate-map figure remain outside Git in
`abi7500-research/captures/abi7500-plr-wellmap-001/`.
USB transcript SHA-256: `27bc8cf4c97b4d3702c3c132789b182c2958d579eed6e8971c659cf19a6c2a9c`.


(abi7500-trayapi-validation)=
## Public plate release, duplicate calls and clean reconnect

The approved `abi7500-plr-trayapi-001` session ran **2026-10-10,
16:52:31.867–16:54:07.332 UTC** from Ubuntu with the same sealed patterned VWR 89218-296
plate. The operator confirmed A1 at the back-left, the drawer closed and hands clear.
PLR executed one **30 °C/60-second hold with the lamp off**, then used the public
`release_plate("plr-trayapi-001")` method. No fluorescence images were collected.

### Completed hold and explicit cleanup

The instrument began inactive at sample/block **25.0/25.0 °C**, cover **105.2 °C**,
filter 0 stationary, lamp off and plate state 0. The driver loaded the internal mechanism once
and sent one prepare/upload/Start sequence. An initial observation spanning startup remained
uncertain until fresh evidence established ownership, without another Start. The hold completed
normally with matching tagged history of **69 seconds**, including the ramp. Holding-phase
sample estimates ranged **29.8–30.2 °C** during initial settling and remained 30.0 °C later;
these readings are not independent temperature calibration.

Public lamp-off/filter-0 cleanup verified the existing states and sent no optical mutation.
`release_plate()` checked the finished run against fresh identity/status/history and optics,
committed release intent, then sent **one `0x72` command**. Status polling observed loaded 2 →
moving 1 → unloaded 0. Dispatch to the first unloaded observation was **5.458 seconds**,
including polling latency. The final run checkpoint recorded `plate_release_state="released"`.

### Repetition and durable ownership

| Check | Result | Additional release commands |
|---|---|---|
| First public release after normal completion | Unloaded confirmed | 1 |
| Repeat call on the same connection | Unloaded confirmed | 0 |
| Repeat after closing USB and creating a fresh driver over the same journal | Unloaded confirmed | 0 |

The fresh connection performed the normal camera/serial-bridge setup; its release check issued
only observation commands. Across both connections the byte audit found exactly one load,
one prepare/upload/Start and one release. There was no Stop, idle-temperature write,
lamp/filter mutation or camera exposure. No communication failure was deliberately induced.

The first durable release write upgraded the live journal from format **1 to 2**. All **12 prior
run records** were unchanged; one completed record was added for this session. The second driver
read the released checkpoint and did not replay motion. This validates clean reconnect and
repetition after confirmed success. It does **not** validate recovery after lost acknowledgements,
process failure during motion, jams or other ambiguous hardware failures; those guards remain
covered by offline tests.

Cleanup ended with inactive state 2, unloaded plate state 0, stationary filter 0 and lamp off,
then closed USB. Last sample/block/cover readings were **30.0/29.9/105.5 °C**. Internal release
does not establish that the external drawer has been opened or that the heated cover has cooled.
No firmware or calibration changes were made.

The executed runtime matched the version with **186 passing package tests**, including 20 plate
status/release tests. Seven additional offline session-runner scenarios covered success,
preflight refusal, interrupted Start/status, lost release reply, wrong reconnect identity and
an aborted run. Full USB transfers, source snapshots, operator confirmation, journal backups and
byte audit remain outside Git in `abi7500-research/captures/abi7500-plr-trayapi-001/`.
USB transcript SHA-256 values:

- First connection: `2b0c03a0ceec4c14088f9a71428daac46beb19d98d4e5828a55147c1e3ff3124`.
- Fresh-driver connection: `94bcef8bb2453e55d165d5beea5cf05b4a5da43e32d50becd2713cf1a95732d0`.


(abi7500-exptable-validation)=
## Stored exposure-table readback from Ubuntu

The approved read-only session `abi7500-plr-exptable-001` ran on **2026-10-12,
13:30:14.710–13:30:16.251 UTC**. PLR connected directly over USB, verified camera identity
`Hamamatsu,C9254-03,2.20,1.05,2.19,007006` and thermal firmware `G2.10`, queried status,
read the stored exposure table twice, queried status again and closed USB successfully.

Both complete 254-byte table payloads matched each other and the earlier vendor capture
byte for byte, including the opaque prefix and tail:

| Physical filter | Stored exposure slots, in instrument order (ms) |
|---|---|
| 1 | 540, 180, 60 |
| 2 | 540, 180, 60 |
| 3 | 900, 300, 100 |
| 4 | 540, 255, 85 |
| 5 (saved channel 1, SYBR/FAM) | 1048, 90, 30 |

The public API returns these durations in **seconds**. It does not write the table or populate
a collection plan. Identical before/after status payloads reported an inactive sequence,
state code 1, sample/block **25.0/25.0 °C**, cover **104.8 °C** and ambient **23.8 °C**.
The cover remained hot even though no thermal program was running.

The audited thermal traffic consisted only of application-mode query `0x2f` (once), version
query `0x0a` (twice), extended status `0x29` (twice) and exposure-table query `0x80` (twice).
Camera/serial-bridge initialization and keepalives were the only other commands. There were no
thermal-run, lamp/filter, plate-motion, image-acquisition, calibration or firmware commands.
The driver opened no run journal; the live journal and its lock file were unchanged.

The first table reply required **142** counted serial reads and the second **132**, including
zero-count polls, one-byte fragments and nonzero stale padding. A complete first-query fixture
is retained losslessly in `test_data/ubuntu_stored_exposure_vectors.json`; offline tests replay
all transfers and verify the resulting table against the independent vendor capture.
All 37 checked source files matched the prepared, tested version before connection; the package
had **195 passing tests** before this session. Full source snapshots, approval, status/table
payloads, byte audit and USB transcript remain outside Git in
`abi7500-research/captures/abi7500-plr-exptable-001/`.
USB transcript SHA-256: `2f9be60448ef7db2b400e2b4f46766e5da2b6423e48702586f691e896162b1b2`.

This verifies readback on this instrument and firmware. It does not establish current calibration
validity or suitable exposures for a particular plate. The earlier patterned-plate session clipped
four fluorescein landmarks at 30 ms, the shortest stored filter-5 exposure. A quantitative comparison
between separate vendor and PLR acquisitions still requires unsaturated signals and matched settings.


(abi7500-comparison-plate-scan)=
## Comparison plate scanned with stored vendor exposures

The approved Ubuntu session `abi7500-plr-compare-001` ran on **2026-10-12,
15:01:00.184–15:03:05.909 UTC**. The operator confirmed a sealed plate, A1 at the back-left,
prepared from **3.2 µM fluorescein stock** with **20 µL per well**:

| Columns, all rows A–H | Nominal contents | Wells |
|---|---|---|
| 1–5 | 32 nM fluorescein | 40 |
| 6–10 | 80 nM fluorescein | 40 |
| 11–12 | Diluent blanks | 16 |

PLR verified the stored filter-5 exposure table before actuation, then executed one
**30 °C/60-second hold** with three images in order **1048, 90 and 30 ms**. The lamp warmed
for 30 seconds at stationary position 0 and power 1. Collection began with 44 seconds left
in the hold; native homing finished with 28 seconds left. Image acquisition spanned
**5.114 seconds**, with sample readback **30.0 °C** before and after every image. Matching
history confirmed normal completion with a 69-second program duration including the ramp.

### Saturation and independent extraction

| Exposure | Native saturated wells | Wells containing clipped pixels |
|---|---|---|
| 1048 ms | 35 | 35 |
| 90 ms | 0 | 0 |
| 30 ms | 0 | 0 |

At 1048 ms, saturation affected 33 of the 80 nM wells, F3 in the 32 nM group and the nominal
blank D12. All candidates are retained. The vendor-compatible selector chose 1048 ms for
61 wells and 90 ms for 35, leaving **0 of 96 selected results flagged saturated**.
Independent NumPy cropping and ROI/ring extraction matched all **288 well/exposure results**.

The expected concentration groups are visible. At the wholly unclipped 90 ms exposure,
median ring-subtracted mean pixel signals were **83.28** counts for 32 nM, **222.47** for
80 nM and **16.43** for blanks. F3 and D12 had unusually strong localized signals; their cause
is unresolved. These observations do not establish concentration accuracy, plate uniformity or
current calibration validity. The same sealed plate was retained for the separately acquired
vendor comparison below. Both scans are unclipped at 30 ms; the vendor scan clipped C6 at 90 ms.

### Cleanup and evidence

The byte audit found one prepare/upload/Start, one plate load, three CJ/CC/CD sequences,
one filter selection, one native home and one public journaled plate release. There was no
Stop command, idle-temperature write, acquisition replay, firmware or calibration change.
Cleanup confirmed lamp off, stationary filter 0 and unloaded mechanism state 0, then closed USB.
Final sample/block/cover readings were **30.0/29.9/105.3 °C**; the cover remained hot.
All 13 prior journal run records were unchanged, with one completed and released run added.

The executed package matched the version with **196 passing tests**; nine offline runner
scenarios covered success, changed exposures, unexpected state and failure cleanup.
The portable `plr-compare-001-results.zip` export retained all three camera transfers byte for
byte, 96 selected results and 288 exposure candidates, with no USB operations or journal change.
Source snapshots, original images, calibration inputs, plate confirmation, raw per-well CSV,
the scan figure and full audit remain outside Git in
`abi7500-research/captures/abi7500-plr-compare-001/`.
USB transcript SHA-256: `9331d01e7f39bd20571cc0e795fa32426db7c3399c0ad8a22b83c0be315842ee`.


(abi7500-fresh-vendor-comparison)=
## Fresh vendor acquisition and paired fluorescence comparison

The authorized Windows session `abi7500-vendor-compare-001` used the same sealed comparison
plate after the operator transferred USB from Ubuntu to the Windows control PC. The saved v2.3
experiment was independently checked before Start: one stage, one repetition, one
**30 °C/60-second hold**, collection on hold only, Standard mode, 20 µL and 105 °C cover.
The vendor application recorded its run from **2026-10-12, 15:23:01.336–15:25:42.195 UTC**,
including preparation and cleanup. The original experiment was preserved; a separate template
and working experiment contained this method and the operator's plate description.

### Same-image processing agreement

USBPcap retained **31 complete 157,312-byte camera transfers**: 16 before the native thermal
Start and 15 during the single collection. The latter comprise three exposures for each of five
physical filters. Filter 5 used **1048, 90 and 30 ms**, matching the PLR scan. All 10,212 USB
records were complete and formed 5,106 submission/completion pairs without transfer errors.
All **465 thermal request/reply pairs** were matched chronologically and against the vendor log,
with no reported thermal errors.

PLR reconstructed the final collection from these original transfers and matched:

- **480/480 raw integers** stored in the vendor EDS, covering all 96 wells and five channels.
- **480/480 corrected binary32 values** from fresh execution of the original vendor Java
  uniformity code. This checks the software correction, not an independent physical standard.
- **1,440/1,440 well/exposure extractions** against an independent NumPy crop and ROI/ring calculation.

The five masks, native `Background.csv` and `uniformity.ini` copied from the current Windows
installation matched the archived PLR inputs byte for byte. No calibration or exposure table
was changed. The vendor still reported expired calibration and invalid RNase P status; these
engineering measurements do not establish current calibration validity.

### Separate-acquisition differences

The vendor's 1048 ms filter-5 frame clipped 34 wells, its 90 ms frame clipped C6, and its 30 ms
frame clipped none. Exposure selection produced **zero saturated selected results** across all
480 values. For filter 5 it selected 1048 ms for 62 wells, 90 ms for 33 and 30 ms for C6.

Both PLR and vendor **30 ms** frames are unclipped in all 96 wells. Comparing ring-subtracted
mean pixel counts at that exposure, without calibration correction or outlier exclusion:

| Nominal group | Wells | PLR median | Vendor median | Median paired difference, vendor vs PLR |
|---|---|---|---|---|
| Blank | 16 | 5.88 | 5.40 | −3.40% |
| 32 nM fluorescein | 40 | 28.07 | 26.89 | −3.57% |
| 80 nM fluorescein | 40 | 74.97 | 71.20 | −5.69% |

Large local differences remain: vendor/PLR signal ratios were **11.72 at F4**, **9.17 at C6**
and **0.44 at blank D11**. All wells remain in the reported statistics. These are descriptive
results from one pair, without a predefined agreement tolerance. The acquisitions have different
preparation and illumination histories; their overall agreement must not be inferred from group
medians alone. Exact same-image reconstruction rules out a discrepancy in the tested processing
path as the explanation for these particular differences. Their physical or acquisition cause
remains unresolved. Ct, dye unmixing and concentration calibration were not validated.

### C6 and F4 pixel investigation

An offline investigation of all 34 original paired-session images (three PLR and 31 vendor)
localized the difference to the camera pixels. At 30 ms, the ring-subtracted mean increased
from **135.83 to 1245.87** at C6 and **34.14 to 400.24** at F4. C6 developed a broad bright
region and F4 a bright crescent. The surrounding-ring mean changed by only 4.16 and 1.59
counts respectively, so ring subtraction does not explain the increases. Neither 30 ms image
clipped any well pixels. F4's vendor 90 ms signal was **2.999 times** its 30 ms signal; C6's
90 ms image clipped and must not be used to assess proportionality.

The large anomalies also appeared between the vendor's own preparation and collection images.
Dividing each well's 30 ms net mean by the median of the other 39 wells with the same nominal
concentration gives:

| Well | PLR collection | Vendor preparation | Vendor collection |
|---|---|---|---|
| C6, 80 nM | 1.82 | 1.49 | 17.61 |
| F4, 32 nM | 1.24 | 1.19 | 14.91 |

This normalization is descriptive, not a calibration or a controlled comparison of preparation
and collection. Preparation used native lamp enable/power **1/0**, preceded the captured plate
load, and had a preceding inactive 25 °C sample estimate. Collection followed lamp power **1**,
plate load, wheel home and the thermal Start, with a preceding 30 °C holding estimate. The
preparation frames contain plate signal and are **not established dark references**. These
simultaneous changes prevent attribution to loading, heating, lamp power or homing individually.

The C6 bright region and F4 crescent occur across all five vendor channels, with unusually strong
non-FAM-channel signal compared with same-concentration peers. These are exposure-normalized
camera channels, not calibrated emission spectra. The other 78 nominal dye wells, excluding
these two targets, have a median within-well image correlation of **0.9925** between the paired
30 ms images. A common-translation fit changes alignment by only about **0.13 row/0.06 column
pixels**. This argues against a gross image shift or incorrect filter as the sole explanation;
it does not exclude a small optical-path change affecting a local reflection. Both locations were
weak in the October 10 patterned-plate images when those wells were empty, which also does not
exclude new contamination or plate-dependent optics.

The leading working explanation is a local optical effect involving liquid, a bubble, the seal
or well surfaces. Fluorescent contamination remains possible. The images do not identify the
physical object or prove a root cause. Photobleaching alone does not explain these large
increases. No calibration coefficients, labels or measurements were changed, and no wells were
silently removed from the paired results. Original-pixel plots, channel comparisons, state
readbacks and reproducible scripts remain outside Git in
`abi7500-research/analyses/abi7500-c6-f4-20261012/`. The re-extraction matched all **1,728**
previously recorded PLR/vendor well-exposure rows exactly; the analysis accessed no hardware.

### Completion and retained evidence

The capture contains one method upload, one native Start, one load and one release, with no Stop
or exposure-table write. The EDS records `COMPLETE`. Final readbacks show inactive state 2,
unloaded mechanism 0, sample/block/cover **30.0/29.8/105.0 °C**, and lamp enable/power words
**1/0**. The final filter-0 command was acknowledged and motor polling reached stationary;
the vendor did not query filter position again afterward. The application and capture process
were closed. The heated cover was still hot at the last readback.

Original frames, PCAP, logs, before/after EDS files, current calibration copies, per-well CSVs,
Java reference output, paired comparison figure and audit remain outside Git in
`abi7500-research/captures/abi7500-vendor-compare-001/`.
PCAP SHA-256: `52dc86a80c5a5a627c7566d0b65193ee37f323377063f0455708db26efed0a84`.
Completed EDS SHA-256: `b78f39a6da2ec3a2ba328a87283d9920f91b6134609ce4ae53a21e932eeb9a82`.

(abi7500-c6-f4-repeat)=
## C6 and F4 short-exposure repeat

The approved Ubuntu session `abi7500-plr-c6f4-001` ran **2026-10-12,
16:12:38.479–16:16:45.052 UTC**. The operator confirmed the same sealed comparison plate
reloaded with A1 at the back-left, the drawer closed and hands clear. The plate had been removed
since the paired vendor scan; this session therefore cannot recover an undisturbed repeat of
that acquisition. The vendor application was closed and USB transferred to Ubuntu.

One thermal Start executed **three consecutive 30 °C/60-second holds**. Each hold collected
**30, 10 and 5 ms** images at physical filter 5, for **nine frames**. Lamp power was 1, with
30 seconds of warmup at position 0. The plate stayed loaded throughout the three collections.
Each burst ended with one native home and stationary position-0 readback, retaining lamp power
1. No calibration or exposure table was changed. Initial lamp enable/power was the vendor's
idle **1/0**; the setter changed power to 1 without redundantly sending enable.

All nine original transfers were complete, all before/after sample estimates were **30.0 °C**,
and **no well pixels clipped at any exposure**. Burst spans were 3.75, 3.93 and 3.82 seconds.
Collection plus homing finished with 30, 30 and 29 seconds remaining in the corresponding hold.
Tagged history recorded normal completion after **191 seconds**, including the initial ramp
and transitions; programmed holds totaled 180 seconds. The transcript contains one method
upload, one Start, one load, three homes and one release, with no cancellation or retry.

### What the repeat establishes

At 30 ms, ring-subtracted mean camera counts were:

| Well | Earlier PLR | Earlier vendor | Repeat 1 | Repeat 2 | Repeat 3 |
|---|---|---|---|---|---|
| C6 | 135.83 | 1245.87 | 1315.13 | 1248.22 | 1234.56 |
| F4 | 34.14 | 400.24 | 30.18 | 30.67 | 30.71 |

C6's broad hotspot was reproduced by PLR in all three collections. It remained approximately
18–20 times the median of the other 39 nominal 80 nM wells. Its net mean declined **6.13%**
from repeat 1 to 3. Exposure-normalized values from 5, 10 and 30 ms agreed within **1.3%**
when each shorter exposure was compared with its corresponding 30 ms image. These observations
support a persistent light signal in this session rather than a vendor-only computation or
saturation artifact; they do not establish the signal's physical source or concentration.

F4's earlier bright crescent was absent. Its signal was approximately **1.09–1.14 times** its
nominal 32 nM peers, changing **+1.76%** from repeat 1 to 3. F3, another previously bright
location, also fell from the vendor net mean of 118.22 to approximately 35 in this session.
This strengthens the case for changeable local optical features. Handling, elapsed time,
illumination history and acquisition setup were not independently controlled, so the result
does not prove that reseating removed a bubble or changed the seal. Native homing between
these collections did not reproduce the earlier order-of-magnitude C6/F4 changes; a more subtle
interaction between the optical path and plate remains possible.

All 96 wells remain in the saved data. Independent NumPy reconstruction matched **864/864**
well/exposure extractions, and the portable export retained all nine original frames, 288
selected measurements and 864 exposure candidates exactly, with zero USB operations. These
three repeats have no predefined agreement tolerance and do not validate concentration
calibration, Ct analysis or current calibration validity.

### Cleanup and evidence

Final readbacks confirmed inactive state, unloaded mechanism, lamp off and stationary filter 0;
USB was closed. Sample/block/cover temperatures were **30.0/29.8/104.8 °C**, so the cover
remained hot. The 35 captured driver/transport source files matched the previously tested
version with 196 passing package tests. The session runner passed ten mocked scenarios and
duplicate-session rejection before execution. No runtime driver changes were needed.

Original frames, USB transcript, controller and journal evidence, calibration inputs, all-well
tables, plots, export and reproducible scripts remain outside Git in
`abi7500-research/captures/abi7500-plr-c6f4-001/`.

(abi7500-c6-f4-rotation)=
## C6 and F4 plate-rotation diagnostic

The approved Ubuntu session `abi7500-plr-c6f4-rotate-001` ran **2026-10-12,
17:26:31.814–17:30:38.562 UTC**. The operator confirmed the same sealed comparison plate
rotated 180° in its horizontal plane, placing printed A1 at the front-right. Instrument
coordinates and ROI masks remained fixed: physical plate C6 moved to instrument F7, and
physical F4 moved to instrument C9. No centrifugation, seal replacement or liquid change was
requested between the baseline and this scan.

One Start repeated the preceding scan's **three consecutive 30 °C/60-second holds**, each
collecting physical-filter-5 images at **30, 10 and 5 ms**. Lamp power was 1 after a 30-second
warmup at position 0. Each collection ended with native homing and a stationary position-0
readback while the lamp remained warm. All nine original frames were complete, all before/after
sample estimates were **30.0 °C**, and no well pixels clipped at any exposure. Collection plus
homing finished with 29, 29 and 30 seconds remaining in its hold. Matching controller history
reported normal completion after **190 seconds**, including ramp and transitions.

### Hotspot location after rotation

Ring-subtracted mean camera counts at 30 ms were:

| Instrument position after rotation | Physical plate well | Nominal concentration | Repeat 1 | Repeat 2 | Repeat 3 |
|---|---|---|---|---|---|
| C6 | F7 | 80 nM | 1350.36 | 1430.16 | 1354.38 |
| F7 | C6 | 80 nM | 70.03 | 72.77 | 70.16 |
| F4 | C9 | 80 nM | 61.06 | 61.24 | 61.22 |
| C9 | F4 | 32 nM | 25.06 | 25.40 | 25.69 |

The hotspot stayed at **instrument C6**, approximately **21.7–22.6 times** its nominal 80 nM
peer median. Physical C6, now at F7, measured **1.12–1.15 times** its peers. The preceding
unrotated session's final C6 mean was 1234.56. The concentration pattern also reversed as
expected: blanks occupied instrument columns 1–2, 80 nM wells columns 3–7, and 32 nM wells
columns 8–12. This supports the physical orientation mapping and excludes simple reuse of
the preceding full-plate image.

These observations support an **instrument-position-dependent source or optical interaction**.
They do not identify contamination, a camera effect or a particular optical component, and
they do not establish a correction coefficient. Plate handling, elapsed time and illumination
history remain confounds. Physical F4 at C9 and instrument F4 both measured near their
respective concentration peers. Neither reproduced the earlier vendor crescent, but that
feature was already absent in the latest unrotated baseline; its original cause remains
unresolved.

The protocol adapts the plate-rotation diagnostic on page 30 of the
[7500/7500 Fast maintenance guide](https://documents.thermofisher.com/TFS-Assets/LSG/manuals/4387777d.pdf)
to this existing dye plate. It is not a background calibration or a test of current calibration
validity. No calibration coefficients, exposure tables, well labels or runtime code changed.

### Verification and cleanup

All 96 wells were retained. Independent NumPy reconstruction matched **864/864** PLR
well/exposure extractions, using identical ROI, background and uniformity inputs for both
sessions. The portable export preserved all nine original frames, 288 selected measurements
and 864 exposure candidates exactly, without USB operations or journal changes.

The transcript contains one upload, one Start, one load, three homes and one release, with no
cancellation or retry. Final readbacks confirmed inactive state, unloaded mechanism, lamp off
and stationary filter 0; USB was closed. Sample/block/cover temperatures were
**30.0/29.8/104.8 °C**, so the cover remained hot. The 35 driver/transport source files match
the previously tested version. The rotation runner passed ten mocked scenarios and duplicate
session rejection before execution.

Original frames, USB transcript, journal snapshots, baseline inputs, calibration files,
instrument/physical-well mappings, all-well tables, plots and reproducible scripts remain
outside Git in `abi7500-research/captures/abi7500-plr-c6f4-rotate-001/`.

(abi7500-water-background)=
## Water-only background diagnostic

The approved Ubuntu session `abi7500-plr-blank-001` ran **2026-10-12,
17:58:44.066–18:02:51.121 UTC**. The operator confirmed a loaded 96-well plate containing
**20 µL water per well**, printed A1 at the back-left, and hands clear. The preparation plan
requested a fresh plate of the same compatible model, deionized water and a fresh optical seal;
those preparation details were not independently observed. Plate and instrument well labels
agreed in this orientation.

One Start executed **three consecutive 30 °C/60-second holds**, each collecting physical-filter-5
images at **30, 10 and 5 ms**. The lamp warmed for 30 seconds at power 1 and filter position 0.
Each collection ended with native homing and stationary position-0 readback while the lamp
remained warm. All nine original frames were complete, all before/after sample estimates were
**30.0 °C**, and no well pixels clipped at any exposure. Each collection plus homing finished
with 30 seconds remaining in its hold. Matching controller history reported normal completion
after **191 seconds**, including ramp and transitions.

### C6 signal without added dye

Ring-subtracted mean camera counts at 30 ms were:

| Instrument position or summary | Repeat 1 | Repeat 2 | Repeat 3 |
|---|---|---|---|
| C6 | 1164.11 | 1082.22 | 1064.43 |
| Median of the other 95 positions | 6.65 | 6.35 | 6.04 |
| F4 | 8.35 | 7.83 | 7.81 |
| F7 | 10.77 | 9.12 | 9.09 |
| H8 | 74.56 | 70.80 | 64.36 |

C6 remained the brightest well in all three repeats. Its broad bright region is present in the
original camera pixels. The preceding unrotated and rotated dye scans ended with instrument
C6 means of 1234.56 and 1354.38 respectively. Persistence with the water-only plate, together
with the earlier rotation result, supports an **instrument-associated background effect that
does not require the intentionally added fluorescein**. It does not identify a contaminated
component, distinguish block from camera/optical causes, or establish a correction coefficient.
Plate, solution matrix, elapsed time and illumination history also changed between sessions.

C6's exposure-scaled 5/10/30 ms measurements agreed within **1.1%** when each shorter exposure
was compared with its corresponding 30 ms result. Its 30 ms signal decreased **8.56%** from
repeat 1 to 3; that decrease does not identify its mechanism. F4's earlier crescent was absent.
Smaller background regions remained elsewhere, including D4 at **152.46** and H8 at **64.36**
counts in the final repeat. All 96 positions remain included. Negative ring-subtracted means
at short exposures were retained in the diagnostic tables; these values are not calibrated
concentrations, and no detection limit or formal background acceptance threshold was established.

### Verification and cleanup

Independent NumPy reconstruction matched **864/864** PLR well/exposure extractions, using
the same ROI, background and uniformity inputs as both preceding dye scans. The portable
export preserved all nine original frames, 288 selected measurements and 864 exposure
candidates exactly, without USB operations or journal changes. This scan did not update
calibration, firmware, exposure tables, runtime code or well labels; current calibration validity
remains unverified.

The transcript contains one upload, one Start, one load, three homes and one release, with no
cancellation or retry. Final readbacks confirmed inactive state, unloaded mechanism, lamp off
and stationary filter 0; USB was closed. Sample/block/cover temperatures were
**30.0/29.8/105.2 °C**, so the cover remained hot. The 35 driver/transport source files match
the previously tested version. The session runner passed ten mocked scenarios and duplicate
session rejection before execution.

Original frames, USB transcript, journal snapshots, calibration files, both baseline inputs,
all-well tables, exposure comparisons, plots and reproducible scripts remain outside Git in
`abi7500-research/captures/abi7500-plr-blank-001/`.

## C9254-02/2.19 water-plate validation (2026-10-14)

The second Fast Dx, label **275030206**, USB serial **0000000747**, identifies its camera as
`Hamamatsu,C9254-02,2.19,1.05,2.19,1Y0747` and thermal firmware as `G2.10`. Its vendor diagnostic
capture established the same CJ millisecond echo, readiness polling, CD transfer length and
321×245 wire geometry. The reconstructed diagnostic frame matched the vendor image-copy routine.
Optical operations accept this exact model/firmware pair as well as C9254-03/2.20; other pairs
remain blocked. Each physical instrument requires its own persistent journal and calibration.

With explicit authorization and Windows disconnected, `plr-u2water-001` completed three
**30 °C/60-second holds** using the same sealed 20 µL-per-well DI-water plate as the first unit's
fresh-water scan, with printed A1 back-left. Each hold collected physical filter 5 at
**30, 10 and 5 ms**, producing nine complete frames. The lamp settled for 30 seconds before
Start; the wheel homed to position 0 after each collection while the lamp remained warm.
Cover warmup preceded the thermal program. Matching history reported **191 seconds** for the
program including ramps/transitions; all frame-bracketing sample estimates were **30.0 °C**.

The transcript contains **2,234 matched thermal exchanges**, one program upload, one Start,
one load, three collection-end homes and one release, with no transport or firmware errors.
There were exactly nine CJ, CC and CD commands, and no lamp-timer or exposure-table writes.
All nine images match the vendor's isolated native image-copy routine byte for byte
(**691,200 pixels**). No pixels reached 4095. Repeated saved-result retrieval generated no
additional USB traffic. The first instrument's journal remained unchanged.

Final observations confirmed inactive state, unloaded mechanism, lamp off and stationary filter 0;
USB closed. Sample/block/cover temperatures were **30.0/29.8/104.5 °C**, so the cover remained hot.

### Limits of the cross-instrument comparison

The second unit's three 30 ms images show a repeatable localized bright region at approximately
**display column 220, row 110** in the same rotated display convention used by the first-unit
reports. Its brightest local-contrast pixel was at column 223, row 112 in every repeat; the first
unit's C6 peak was at column 143, row 86. Thus the raw image patterns differ. These coordinates
are not verified physical well labels on the second unit. No first-unit ROI, background or
uniformity correction was applied to it, and camera counts across instruments are not calibrated
equivalent fluorescence. This session neither identifies the bright region's cause nor establishes
that the second instrument is free of an optical/background issue.

A unit-specific known-pattern plate remains necessary to validate physical well labels. The
rotation follow-up below tests whether the second bright region follows the plate.
Full captures, original frames, journal snapshots, native-copy checks and raw comparison figures
remain outside Git in `abi7500-research/captures/abi7500-plr-unit2-water-001/`. The device suite
passed 201 tests; the prepared runner passed ten mocked success/failure scenarios. Lint,
formatting, type checking, notebook validation, registry-link validation and the focused ABI
Sphinx build passed. The full documentation build was blocked by an unrelated MicroSpin import.


## C9254-02/2.19 water-plate rotation (2026-10-14)

With separate explicit approval, `plr-u2rot-001` repeated the second-unit scan after the operator
rotated the same water plate 180°. Instrument, camera, filter, exposures, thermal holds and
collection-end homing were unchanged. The three 30 °C/60-second holds produced nine complete
filter-5 frames at 30, 10 and 5 ms. All frame-bracketing sample estimates were 30.0 °C; matching
history reported a normal completion in 190 seconds. The driver/transport source was unchanged.

The dominant bright region stayed at essentially the same image position. In the same display
convention, the mean 30 ms image centroid moved from **row 110.054, column 220.331** to
**row 109.258, column 220.595**, a displacement of **0.839 pixels**. Centroids use the positive
local contrast within the >200-count connected component containing the peak, after subtracting
a 21-pixel median. All nine rotated frames have their strongest local contrast near this position.
This supports a fixed instrument-position contribution; the dominant spot did not move across
the image with the plate. It does not distinguish the block, holder or optical system as the source.
No physical well label is assigned on this unit.

Using the original centroid for both scans, a radius-7-pixel signal disk minus the mean of a
radius-10–14-pixel surrounding ring gave mean 30 ms signals **32.1% higher** after rotation.
These are exploratory image-space values, not calibrated well measurements. Seating, elapsed
time and illumination/thermal history can affect amplitude; the intensity change does not
identify a cause. No first-unit mask or calibration was applied.

All nine reconstructed frames match the isolated vendor image-copy routine exactly
(**691,200 pixels**), with no pixels at 4095. The transcript contains 1,062 matched thermal
exchanges, one upload, one Start, one load, three homes and one release, with no transport errors,
lamp-timer commands or exposure-table writes. Final readbacks confirmed inactive state, unloaded
mechanism, lamp off and stationary filter 0; USB closed. Sample/block/cover temperatures were
**30.0/29.8/105.3 °C**, so the cover remained hot. Prior journal records and the first unit's
journal were preserved. The runner passed ten mocked scenarios and duplicate-session rejection.

Full captures, original frames, baseline frames, journal snapshots, analysis scripts and comparison
figures remain outside Git in `abi7500-research/captures/abi7500-plr-unit2-water-rotate-001/`.


(abi7500-pcr5-validation)=
## Five PCR-temperature cycles with C9254-02/2.19 (2026-10-14)

The explicitly approved `plr-u2pcr5-001` session ran on Fast Dx **275030206**, USB serial
**0000000747**, camera `Hamamatsu,C9254-02,2.19,1.05,2.19,1Y0747`, thermal firmware `G2.10`.
It used the same sealed 20 µL-per-well DI-water plate, still rotated 180° from the earlier
A1-back-left scan. This was a thermal-control and acquisition test, not an amplification experiment.
The driver and USB transport source were unchanged from the preceding water scans.

The method was constructed by the offline `build_pcr_program()` cell in
[Run a PCR plate](pcr-run.ipynb) and executed through the public PLR API by the recorded session
harness. It used the captured Standard/20 µL/block-2/tube-0 configuration:

1. 95 °C for 120 seconds.
2. Five repetitions of 95 °C for 15 seconds, then 60 °C for 60 seconds.
3. 30 °C for 60 seconds, without acquisition.

Each 60 °C hold collected physical filter 5 at **30/10/5 ms** using the 45-second window,
with native homing after each three-image burst. The lamp settled for 30 seconds at filter 0
before Start and remained warm between collections. The observation budget was 1,800 seconds.
The temperatures/durations match the saved Standard-mode SYBR assay, except that its forty
repetitions were reduced to five and an explicit cooling hold was appended.

### Completion and acquisition

Matching instrument history reported normal completion in **870 seconds**, including ramps
and cooling. Every planned hold was observed at its expected group/repetition/step. During the
five 95 °C cycling holds, reported sample estimates ranged **94.4–95.1 °C**, including entry
transients; during the 60 °C holds they ranged **60.0–61.0 °C**. Both status observations bracketing
every image reported **60.0 °C**. These are the instrument's sample estimates and sensors,
not independent thermal calibration measurements.

All **15 frames** were complete, correctly associated with their intended run and hold, and
unsaturated at 4095. Isolated execution of the vendor image-copy routine reproduced all
**1,152,000 pixels** exactly. The notebook's raw-image export helper was also exercised offline
on these real acquisitions: all original transfers and reconstructed images matched byte for byte,
its content hashes verified, and an existing export could not be overwritten. No calibration
or physical well labels were applied to this second unit's frames.

The transcript contains **4,182 matched thermal exchanges**, one upload, one Start, one load,
five homes, fifteen CJ/CC/CD sequences and one release. There were no transport/firmware errors,
Stop commands, retries, exposure-table writes or lamp-timer commands. Saved-result retrieval
issued no additional USB traffic. The prior second-unit runs and first-unit journal were preserved.

Final observations confirmed inactive state, unloaded mechanism, lamp off and stationary filter 0;
USB closed. Sample/block/cover temperatures were **30.0/29.8/104.9 °C**. The cover remained hot.

### Scope and reproducibility

This validates this five-cycle, single-filter water-plate method on the second unit. It does not
validate forty cycles, PCR amplification, Ct, current calibration, physical well mapping on this
unit, or ambiguous communication-failure recovery. The reusable notebook's orchestration passed
nine mocked success/failure scenarios and duplicate-output rejection; the recording harness
passed eleven scenarios, including hot-plate release refusal and uncertain release handling.
Notebook structure, imports, formatting, lint, registry links and the focused Sphinx build were
checked without executing notebook hardware cells. The exact notebook method was used in the
hardware harness; the notebook's orchestration itself was tested with mocks.

A separate forty-cycle configuration matches the saved assay's thermal schedule. Its later
dye-plate validation is recorded below; amplification remains unverified. Named-well fluorescence
export additionally requires this unit's matching mapping and calibration inputs.
Original frames, complete traffic, journal snapshots, temperature trace,
notebook/export checks and the prepared forty-cycle configuration remain outside Git in
`abi7500-research/captures/abi7500-plr-unit2-pcr5-001/`.

(abi7500-pcr40-validation)=
## Forty PCR-temperature cycles with a fluorescein plate (2026-10-14)

The explicitly approved `plr-u2pcr40-001` session ran on Fast Dx **275030206**, USB serial
**0000000747**, camera `Hamamatsu,C9254-02,2.19,1.05,2.19,1Y0747`, thermal firmware `G2.10`.
The operator confirmed the existing sealed dye plate was loaded with A1 back-left, no obvious
evaporation, drawer closed and hands clear. The recorded plate preparation was 20 µL per well:
columns 1–5 at 32 nM fluorescein, columns 6–10 at 80 nM, and columns 11–12 diluent blanks.
This was a full-length software/control test with dye, not a PCR amplification experiment.

The unchanged [PCR notebook](pcr-run.ipynb) method builder supplied the exact program to the
public-API recording harness. The driver and USB transport matched the preceding five-cycle
validation. The captured Standard/20 µL/block-2/tube-0 configuration executed:

1. 95 °C for 120 seconds.
2. Forty repetitions of 95 °C for 15 seconds, then 60 °C for 60 seconds.
3. 30 °C for 60 seconds, without acquisition.

Each 60 °C hold collected physical filter 5 at **30/10/5 ms** within a 45-second window.
Native homing and parked-filter readbacks followed every three-image burst; the lamp remained
warm between collections. The lamp settled for 30 seconds before Start. The observation budget
was 7,200 seconds, and the frozen runner passed eleven offline success/failure scenarios and
duplicate-session rejection before hardware execution.

### Completion and data integrity

Matching instrument history confirmed normal completion in **5,370 seconds (89 minutes
30 seconds)**, including ramps and final cooling. All **82 planned holds** were observed with
the expected group/repetition/step. Instrument-reported sample estimates during 95 °C holds
ranged **94.1–95.1 °C**, including entry transients; 60 °C holds ranged **60.0–61.0 °C**.
Every status observation bracketing an image reported **60.0 °C**. These are instrument
estimates and sensors, not an independent thermal calibration.

All **40 collections and 120 frames** were complete and associated with their intended run
and hold. Each original camera transfer contained 157,312 bytes and reconstructed to a
320 × 240 image; no pixels clipped at 4095. The notebook's raw-export helper preserved every
original transfer and reconstructed image byte for byte, verified content hashes, and rejected
an existing export destination. No unit-specific calibration or physical well labels were applied.
Isolated execution of the vendor image-copy routine matched all **9,216,000 pixels** exactly.

The complete transcript contains **25,277 matched thermal exchanges**, one method upload,
one Start, one load, forty homes, 120 CJ/CC/CD sequences and one release. There were no
transport/firmware errors, Stop commands, retries, exposure-table writes or lamp-timer commands.
Saved-result retrieval generated no additional USB traffic.

Final readbacks confirmed inactive state, unloaded mechanism, stationary filter 0 and lamp off;
USB closed. Sample/block/cover temperatures were **30.0/29.8/105.2 °C**. The heated cover
remained hot after the sample cooled.

### Scope

This demonstrates the complete forty-cycle thermal/acquisition/cooling/release workflow directly
from Ubuntu on this unit. It does not establish amplification, Ct, dye stability, quantitative
assay performance, current calibration or physical well mapping on this second instrument.
The exact notebook method and offline export helper were exercised; its orchestration cells were
not directly executed on hardware. Broader programs and ambiguous communication failures remain
unverified.

Original frames, complete USB traffic, journal snapshots, source hashes, offline checks and the
temperature trace remain outside Git in
`abi7500-research/captures/abi7500-plr-unit2-pcr40-001/`.

(abi7500-hcat-timing-validation)=
## Five cycles with 30-second collection holds (2026-10-14)

The explicitly approved `plr-u2hcat5-001` session ran on Fast Dx **275030206**, USB serial
**0000000747**, camera `Hamamatsu,C9254-02,2.19,1.05,2.19,1Y0747`, thermal firmware `G2.10`.
The operator confirmed the same sealed dye plate was loaded with A1 back-left, drawer closed
and hands clear. Its recorded preparation was 20 µL per well: columns 1–5 at 32 nM fluorescein,
columns 6–10 at 80 nM, and columns 11–12 diluent blanks.

The user's hcaT method specifies 95 °C for 120 seconds, followed by forty repetitions of
95 °C for 10 seconds and 60 °C for 30 seconds. This engineering test used **five repetitions**
of those cycling steps and appended 30 °C for 60 seconds. It retained the captured
Standard/20 µL/block-2/tube-0 configuration; the assay screenshot does not establish its
instrument mode. The public-API harness adapted the notebook method's step durations without
changing the driver or USB transport.

Each 60 °C hold collected physical filter 5 at **30/10/5 ms**, with a **20-second read window**
and native homing afterward. The lamp settled for 30 seconds at filter 0 before Start and
remained warm between collections. The observation budget was 1,800 seconds. Before execution,
the runner passed eleven mocked success/failure scenarios and duplicate-session rejection;
all 36 collection tests passed. Structural checks rejected the previous 45-second window for
these shorter holds and accepted the 20-second window.

### Timing and acquisition

Matching instrument history confirmed normal completion in **694 seconds (11 minutes
34 seconds)**, including ramps and final cooling. All **12 planned holds** were observed.
Instrument-reported sample estimates ranged 94.1–95.1 °C during 95 °C holds and 60.0–60.9 °C
during 60 °C holds, including entry transients. Status observations bracketing every image
reported **60.0–60.1 °C**. These are instrument estimates, not independent thermal calibration.

The collection records bracket the entire three-image burst and subsequent homing. All five
records remained in the intended run, cycle and hold:

| Cycle | Hold remaining before collection | Collection including homing | Hold remaining afterward |
| --- | --- | --- | --- |
| 1 | 19 s | 12 s | 7 s |
| 2 | 20 s | 13 s | 7 s |
| 3 | 19 s | 12 s | 7 s |
| 4 | 20 s | 13 s | 7 s |
| 5 | 20 s | 13 s | 7 s |

These timings use instrument status counters, with one-second resolution. All **15 frames**
were complete and unclipped at 4095. Each 157,312-byte original transfer reconstructed to a
320 × 240 image. Isolated execution of the vendor image-copy routine matched all **1,152,000
pixels** exactly. The notebook's offline raw-export helper preserved the transfers and images
byte for byte and rejected an existing output destination. No unit-specific calibration or
physical well labels were applied.

The transcript contains **3,372 matched thermal exchanges**, one method upload, one Start,
one load, five homes, fifteen CJ/CC/CD sequences and one release. There were no transport or
firmware errors, Stop commands, retries, exposure-table writes or lamp-timer commands.
Saved-result retrieval generated no additional USB traffic.

Final readbacks confirmed inactive state, unloaded mechanism, stationary filter 0 and lamp off;
USB closed. Sample/block/cover temperatures were **30.0/29.8/104.7 °C**. The cover remained hot.

### Scope

This supports three short exposures and homing within the tested 30-second holds on this unit.
The 7-second margin is observed evidence for this configuration, not a guarantee for other
filters, exposures, computers or run lengths. The earlier forty-cycle test used longer holds;
a full forty-cycle run with this shorter schedule remains unverified.

The intended assay uses DNA, Meridian MDX024 and EvaGreen, with no reverse-transcription step.
This session used fluorescein only and does not validate amplification, Ct, EvaGreen exposure
selection, current calibration or physical well mapping on this second instrument. The assay's
passive-reference setting and Standard/Fast mode remain to be confirmed.

Original frames, complete USB traffic, timing audits, journal snapshots, source hashes,
offline checks and the temperature trace remain outside Git in
`abi7500-research/captures/abi7500-plr-unit2-hcat5-001/`.

(abi7500-unit2-wellmap-validation)=
## Second-unit orientation and six physical well labels (2026-10-14)

The explicitly approved `plr-u2map-001` session ran on Fast Dx **275030206**, USB serial
**0000000747**, camera `Hamamatsu,C9254-02,2.19,1.05,2.19,1Y0747`, thermal firmware `G2.10`.
The operator confirmed the prepared six-marker plate: **A1, A4, C9, F3, H7 and H12**, each
containing 20 µL of 3.2 µM fluorescein, with the other 90 wells empty, A1 back-left, seal intact,
drawer closed and hands clear.

PLR executed one **30 °C/60-second hold**, collecting physical filter 5 at **5/10/30 ms**,
with one native home afterward. The lamp settled for 30 seconds at filter 0 before Start.
The collection window was 45 seconds; the observation budget was 1,800 seconds. Matching
instrument history confirmed normal completion in **69 seconds**, including ramping.
Collection and homing took 13 instrument seconds, leaving 32 seconds in the hold. Every
status observation bracketing an image reported a sample estimate of **30.0 °C**.

### Independent geometry and marker checks

The retained Windows backup has no matching vendor well-mask or correction files for this unit.
Before this scan, an exploratory 8 × 12 grid was therefore fixed from the second unit's five
earlier 30 ms dye images in `plr-u2hcat5-001`. Local-contrast centroids supplied a quadratic
grid fit with 87 accepted positions; the maximum retained fit residual was 1.45 pixels.
Each predicted position received a circular measurement region of radius four pixels.
The grid, analysis code and source-image hashes were frozen before hardware execution.
No first-instrument mask or calibration was transferred.

**All six expected locations were the six brightest regions in every image**, both by raw mean
and by mean minus a surrounding annulus. No positions, labels or coefficients were adjusted
using this marker scan. Row reversal and column reversal each matched zero markers; 180°
reversal and column-major indexing each matched two, versus six for the expected mapping.
Five synthetic orientation cases had checked those distinctions before execution.

| Exposure | Filled-region raw mean range | Empty-region raw mean range | Clipped pixels in the complete image |
| --- | --- | --- | --- |
| 5 ms | 325.52–1213.60 | 129.96–192.78 | 0 |
| 10 ms | 520.21–2313.66 | 131.84–253.65 | 0 |
| 30 ms | 1288.21–3953.51 | 136.45–492.57 | 151 |

The two shorter images provide unclipped marker evidence. The complete 30 ms frame and all
clipping flags are retained. Of its 151 clipped pixels, 42 fall inside the small A1 measurement
region; the table's full-image counts must not be confused with region-only counts.
Independent integer sums checked all **288 region/exposure measurements**. Isolated execution
of the vendor image-copy routine reproduced all **230,400 image pixels** exactly.

A separate global component detector, independent of the grid regions, found the same six
strongest markers in both unclipped images. Their centroids lay approximately **0.23–3.57 pixels**
from the fixed grid centers. This supports the location assignments while also showing why
these small exploratory regions are not validated quantitative vendor masks.

### Completion and scope

The transcript contains **447 matched thermal exchanges**, one upload, one Start, one load,
one home, three CJ/CC/CD sequences and one release. There were no transport or firmware errors,
Stop commands, retries, calibration writes, exposure-table writes or lamp-timer commands.
Final readbacks confirmed inactive state, unloaded mechanism, stationary filter 0 and lamp off;
USB closed. Sample/block/cover temperatures were **30.0/29.9/105.3 °C**. The cover remained hot.

This validates the physical orientation and these six locations in filter 5 on this unit.
It does not independently establish every other well label, vendor ROI boundaries, background
or uniformity correction, concentration accuracy, other filters or PCR performance. Empty wells
are not solvent-matched blanks. The existing public camera-to-well processing path still requires
explicit matching vendor masks and calibration inputs; this diagnostic grid does not replace them.

Original frames, full traffic, journal snapshots, frozen grid and its earlier source images,
raw region measurements, alternative-orientation scores and reproducible analysis remain outside
Git in `abi7500-research/captures/abi7500-plr-unit2-wellmap-001/`.

(abi7500-dual-timing-validation)=
## Two-filter collection within 30-second holds (2026-10-14)

The explicitly approved `plr-u2dual-001` session ran on Fast Dx **275030206**, USB serial
**0000000747**, camera `Hamamatsu,C9254-02,2.19,1.05,2.19,1Y0747`, thermal firmware `G2.10`.
The operator confirmed the existing dye plate was inserted with A1 back-left. This was a timing
and transport check; it did not use a confirmed ROX-positive control or PCR reaction.

PLR executed **three repetitions of 60 °C for 30 seconds**, then **30 °C for 60 seconds**.
Each collection used physical filter 2 at **540/180/60 ms**, followed by physical filter 5 at
**30/10/5 ms**, then native homing to stationary position 0. The read window was 25 seconds.
Lamp power 1 settled for 30 seconds before Start and remained enabled between collections.
The observation budget was 1,800 seconds. Filter 2 exposures matched the saved table read during
preflight; the table was not changed. Twelve offline runner scenarios and seven mocked
two-filter transport tests covered completion and failure handling before execution.

An initial USB setup attempt failed because the serial selector checked an uninitialized PyUSB
serial cache. It sent no instrument commands and reserved no run. Address-based selection,
guarded by the Linux USB serial and full camera/thermal identity checks, connected successfully
for the same approved session. Both attempts and their source hashes are retained separately.

### Timing and image integrity

Matching instrument history confirmed normal completion in **203 seconds**, including ramps
and final cooling. All **three collections and eighteen frames** completed in their intended
run and hold. Status observations bracketing every image reported **60.0 °C** sample estimates.

| Collection | Hold remaining before collection | Collection including homing | Hold remaining afterward |
| --- | --- | --- | --- |
| 1 | 25 s | 19 s | 6 s |
| 2 | 24 s | 18 s | 6 s |
| 3 | 24 s | 18 s | 6 s |

The prepared acceptance criterion required at least three reported seconds remaining after each
home. All three exceeded it. These counters have one-second resolution, and the observed margin
is not a worst-case guarantee for different hosts, exposures or longer runs.

Each 157,312-byte original transfer reconstructed to a 320 × 240 image. Independent array
extraction and isolated execution of the vendor image-copy routine matched all **1,382,400
pixels** exactly. Full-image clipping counts were:

| Physical filter | Exposure | Pixels at 4095 in collections 1 / 2 / 3 |
| --- | --- | --- |
| 2 | 540 ms | 194 / 194 / 194 |
| 2 | 180 ms | 28 / 31 / 35 |
| 2 | 60 ms | 0 / 0 / 0 |
| 5 | 30, 10 and 5 ms | 0 in every frame |

All original frames and clipping evidence are retained. These are image-wide counts, not
well-level saturation decisions. The filter 2 signal does not establish ROX fluorescence on
this plate.

### Completion and scope

The transcript contains **1,103 matched thermal exchanges**, one method upload, one Start,
one load, three homes, eighteen CJ/CC/CD sequences and one release. There were no transport or
firmware errors, Stop commands, exposure-table writes or lamp-timer commands. Cached progress
and result retrieval generated no additional USB traffic. The six preceding second-unit run
records and the first-unit journal were unchanged.

Final readbacks confirmed inactive state, unloaded mechanism, stationary filter 0 and lamp off;
USB closed. Sample/block/cover temperatures were **30.0/29.9/105.3 °C**. The cover remained hot.

This establishes two-filter acquisition and homing within these three 30-second holds on this
unit. A full forty-cycle two-filter method, ROX-positive response, filter 2 physical well mapping,
calibrated green/reference measurements, Rn and PCR performance remain unverified. Filter 5
diagnostic regions must not be transferred to filter 2 without independent evidence.

Original frames, full USB traffic, timing and image-copy audits, approval, source snapshots and
journals remain outside Git in `abi7500-research/captures/abi7500-plr-unit2-dual-001/`.

(abi7500-rox-layout-validation)=
## ROX/fluorescein layout and two-filter raw export (2026-10-14)

The explicitly approved `plr-u2rox-001` session ran on Fast Dx **275030206**, USB serial
**0000000747**, camera `Hamamatsu,C9254-02,2.19,1.05,2.19,1Y0747`, thermal firmware `G2.10`.
The operator confirmed the prepared plate was loaded with A1 back-left and the drawer closed.
Each well contained 20 µL, with the following asymmetric layout:

| Preparation group | Confirmed fluorescein / ROX (nM) | Wells |
| --- | --- | --- |
| Fluorescein only | 32 / 0 | A2, A5, C10, F4, H8, H11 |
| Lower ROX only | 0 / 500 | A1, A4, C9, F3, H7, H12 |
| Modified higher ROX only | 0 / 1,470.588 | B1, B4, D9, G3, G7, G12 |
| Fluorescein + lower ROX | 32 / 500 | A3, A6, C11, F5, H9, H10 |
| Fluorescein + modified higher ROX | 37.647 / 1,470.588 | B3, B6, D11, G5, G9, G10 |
| Water | 0 / 0 | Remaining 66 wells |

The two modified tubes used **10 µL of 25 µM ROX stock** and remained at **170 µL total**,
giving **1,470.588 nM ROX**. The mixed tube also contained 2 µL of 3,200 nM fluorescein,
giving **37.647 nM fluorescein**. The other fluorescein preparations followed the 32 nM recipe.
The operator also confirmed that the lower-ROX tubes used **4 µL directly from the 25 µM stock**
in **200 µL total**, giving **500 nM ROX**. The proposed working dilution was not made. Historical
group identifiers `R50` and `F_R50` therefore denote the confirmed 500 nM preparations, not 50 nM.
The concentration amendment records this correction without changing original images, measured
values, label evidence or their hashes. The plate contains no confirmed 50 nM ROX condition.
These preparation calculations do not establish concentration-response or assay performance.

### Control and data integrity

PLR executed one **30 °C/60-second hold**. Physical filter 2 collected **540/60/5 ms** exposures,
then filter 5 collected **270/90/30 ms**, followed by one native home. The shorter ROX bracket
was selected before execution after the preparation clarification. The lamp settled for
30 seconds at power 1 and position 0; the read window was 45 seconds and observation budget
1,800 seconds. The revised runner passed twelve offline success/failure scenarios, approval
and source checks, and duplicate-execution rejection. Thirty-two relevant device tests passed.

Matching history confirmed normal completion in **68 seconds**, including ramping. All six
frames belonged to the intended hold, with sample estimates of 30.0 °C before and after each
image. Collection and homing consumed **18 instrument seconds**, from 44 to 26 seconds remaining.
Every 157,312-byte original transfer reconstructed to a 320 × 240 image; independent array
extraction and isolated vendor image-copy execution matched all **460,800 pixels** exactly.

| Physical filter | Exposure | Full-image pixels clipped at 4095 |
| --- | --- | --- |
| 2 | 540 ms | 5,032 |
| 2 | 60 ms | 1,890 |
| 2 | 5 ms | 0 |
| 5 | 270 ms | 171 |
| 5 | 90 ms | 27 |
| 5 | 30 ms | 0 |

The transcript contains **445 matched thermal exchanges**, one upload, one Start, one load,
one home, six CJ/CC/CD sequences and one release. No transport or firmware error, Stop,
exposure-table write or lamp-timer command occurred. Final readbacks confirmed inactivity,
unloaded mechanism, stationary filter 0 and lamp off; USB closed. Sample/block/cover
temperatures were **30.0/29.9/104.7 °C**. The cover remained hot. All seven previous second-unit
run records and the first-unit journal were unchanged.

### Spatial evidence and limitations

Before execution, the earlier filter-5 grid was frozen as an **unverified image-space hypothesis
for filter 2**. It was not adjusted to this plate. At all three ROX exposures, the 24 strongest
candidate regions, by both raw mean and local-annulus contrast, matched **all 24 intended
ROX-containing positions**. Row reversal and column reversal each matched two; 180° reversal
matched sixteen and column-major indexing matched five. The 5 ms frame supplied unclipped
evidence. Its ROX-only lower group had local contrasts of 63.87–162.99 counts, versus
−0.55–4.87 in water regions and 0.31–1.52 in fluorescein-only regions. These are exploratory
contrast statistics, not vendor-corrected fluorescence.

The prepared global component detector imposed a maximum area of 200 pixels, excluding most
observed ROX spots. Its failed check is retained. A separate **post-scan exploratory check**
removed that ceiling without changing the threshold or grid. The strongest 24 components
matched the expected locations, with centroids **0.32–2.29 pixels** from the fixed centers and
areas **180–248 pixels**. This corroborates localization in the same data; it is not another
prospective validation or evidence for quantitative region boundaries.

The green channel did not exactly match the intended ranking. In every exposure, extra signal
at **D9** (ROX-only) and **B12** (water) displaced **C10** and **H11** from the brightest eighteen
candidate regions. C10 and H11 still had positive local contrast. The independent component
ranking in the unclipped 30 ms image reproduced those discrepancies. Their causes remain
unresolved; no regions or values were adjusted to hide them.

### Public raw measurement export

The filter-2 diagnostic region object records this scan's evidence for **24 physical labels**.
It retains the unchanged candidate coordinates and radius-four pixel selections; all boundaries
and the other 72 labels remain provisional. Filter 5 retains its earlier six-label evidence.
No first-unit mask or calibration was used.

`write_raw_run_archive()` exported **576 rows** (six exposures × 96 regions), and every pixel
count, sum, mean, minimum, maximum and clipping statistic matched the independent calculation.
All six original transfers and eighteen manifest-member hashes were verified, and a duplicate
destination was rejected without altering the archive. USB construction/read/write were
disabled during this offline export check. Every row retains `regions_provisional=True` and
`calibration_applied=False`; saturated exposures are preserved alongside unclipped ones.

This supports ROX detection, the listed physical locations and a raw two-filter export on this
unit. It does not establish calibrated dye concentrations, quantitative well boundaries,
spectral unmixing, reporter/reference ratios, Rn, Ct or PCR performance. The full forty-cycle
two-filter assay schedule remains unverified. Captures, plate metadata, all analyses and the
public raw archive remain outside Git in `abi7500-research/captures/abi7500-plr-unit2-rox-001/`.
The confirmed plate metadata and concentration-annotated exports are in
`abi7500-research/analysis/abi7500-unit2-rox-001-concentration-update/`, with a separate amendment
archive beside the original capture ZIP. The original capture remains unchanged for provenance;
the amendment supersedes its nominal concentrations and pending-confirmation notes.

## Second-unit vendor comparison preparation (2026-10-15)

The operator authorized a same-plate vendor comparison with Fast Dx **275030206** connected
to Windows. Windows enumerated Hamamatsu USB serial **0000000747**. The already-open vendor
application displayed **Disconnected**; its saved instrument preference selected 275030206.
The open experiment was preserved under a separate filename before closing the application.
No acquisition or thermal program was started during this preparation.

The Windows instrument configuration, native masks and application configuration were copied
and verified against SHA-256 hashes. The directory for **275030206** contained only
`SystemConfiguration.sdc`: no spatial, background, uniformity or pure-dye calibration files.
The installation retained calibration files under the first instrument's **275032266** directory.
Those files do not establish calibration for the second instrument and were not transferred
into its profile.

The file search covered the Windows installation, the user's Documents and Downloads folders,
and 143 EDS files. Ninety-one EDS files contained embedded calibration tables, but the inspected
text metadata did not establish a link to serial 275030206. Their presence alone is insufficient
provenance for restoring the second instrument's calibration.

The v2.3 calibration manager loads calibration files from the selected serial-number directory.
Its experiment pre-run validator rejects missing spatial, background or pure-dye calibration;
this differs from the confirmation offered for expired calibration. A comparison against
vendor-corrected per-well results therefore requires this unit's own calibration inputs first.
Raw PLR camera acquisition and the earlier diagnostic-region export remain separate capabilities.
Preparation records and backups are retained outside Git in
`abi7500-research/captures/abi7500-unit2-rox-vendor-001/`.

## Second-unit vendor ROI calibration (2026-10-15)

The operator explicitly authorized one ROI session, including all five filters, on Fast Dx
**275030206**, Hamamatsu C9254-02 USB serial **0000000747**. The prepared uniform mixed-dye plate
was loaded with A1 back-left, the drawer closed and hands clear. Windows v2.3 performed the
calibration while USBPcap recorded its traffic. An initial manual setup was canceled at the
heated-cover temperature warning without acquiring an image or saving a mask. The guided
procedure set the cover to 90 °C and completed from **08:48:13 to 08:50:42 EDT**.

The vendor reported successful calibration for **E, D, C, B and A**, saved all five results,
lowered the block and displayed **Current** for every ROI filter. Filters **D and A** additionally
reported warning 101, “Lower than normal exposure detected.” Background, optical and system-dye
calibration remained **Not Calibrated**, and RNase P remained **Not Run**.

PLR parsed all five committed masks, each containing **96 nonempty well regions**. Their complete
serialized bytes occur in this unit's new `Calibration.sda`. Each saved vendor TIFF matched a
captured frame reconstructed by `CameraFrame.image_bytes` and rotated 180°, with **zero pixel
differences across 384,000 pixels**. This confirms the TIFF/image-coordinate relationship;
the uniform plate does not independently establish all physical well labels.

| Native file filter | Vendor letter | Final exposure (ms) | Pixels at 4095 |
| --- | --- | --- | --- |
| F0 | E | 540 | 121 |
| F1 | D | 492 | 154 |
| F2 | C | 756 | 153 |
| F3 | B | 404 | 96 |
| F4 | A | 300 | 143 |

The clipped pixels are retained in the original images; a passing ROI result does not establish
unclipped quantitative fluorescence. Overlay inspection confirmed that the masks follow the
96 visible well regions in each saved image.

The capture contains **6,821 matched USB exchanges** and **45 complete 157,312-byte camera
transfers**, with no incomplete records, unmatched requests or USB completion errors. USBPcap
required stopping its verified process after the vendor application had disconnected; the
resulting capture passed those integrity checks. All **855** files in the before/after backups
passed independent SHA-256 verification. The first unit's configuration was unchanged.
Vendor and capture processes were closed. Full backups, images, masks, logs, screenshots and
offline verification remain outside Git in
`abi7500-research/captures/abi7500-unit2-roi-001/`.

These results establish this unit's saved vendor ROI calibration and PLR's ability to read its
masks and reproduce its ROI images. Background, optical and dye calibration, followed by the
vendor/PLR fluorescence comparison, remain necessary for calibrated per-well results.

## Second-unit vendor background calibration failure (2026-10-15)

The operator authorized one background session on Fast Dx **275030206**, camera USB serial
**0000000747**, with the water plate loaded, A1 back-left and hands clear. The requested plate
preparation was 20 µL water per well with an optical seal; this was not a vendor-supplied buffer
plate. Windows v2.3 ran its background procedure from **09:19:01 to 09:29:32 EDT**, including
the nominal ten-minute 60 °C hold. Image collection finished at 09:26:18.

The thermal run completed, but the calibration wizard reported **Failed**, with possible block
contamination. Its fluorescence thresholds and failing vendor well labels were:

| Vendor filter | Threshold (FSU) | Wells above threshold |
| --- | --- | --- |
| A | 72,000 | A6, A7, A9, A11, B1, B10, B12, C1, C2, C3, C4, C6, C8, C10, D1, D6, D9, E1, E6, E11, F1, F5, F11, G2, G4, H1, H6, H8, H12 |
| B | 72,000 | B12, D9 |
| C | 72,000 | B12, D9 |
| D | 72,000 | B12, D9 |
| E | 90,000 | A9, D9 |

Native extraction also reported warning 200: outliers were removed and compensation applied at
**B12**. This did not make the calibration pass. PLR parsed all **960** generated background
coefficients; evaluating their diagnostic 1,000 ms values reproduced every above-threshold
well list. D9 was approximately **745,000–857,000 FSU** across the five filters. These failed
coefficients must not be used as validated fluorescence calibration. The result alone does not
distinguish plate contamination, instrument contamination or another optical problem.

The capture retained **226 complete camera transfers**, **45,610 matched USB exchanges** and
**500 chronologically paired thermal exchanges**, with no transfer errors or missing log
counterparts. Native sequence numbers were reused after worker resets, so pairing used capture
order rather than a session-wide sequence lookup. The captured exposure commands included
500, 1,000 and 2,000 ms; the PLR snapshot API's exposure limits were not changed by this session.
The five ROI mask files were unchanged, and their complete bytes remained in the generated
calibration container. All **863** before/after backup and experiment files passed hash checks.

The wizard was canceled without selecting **Save Results**, and the maintenance manager still
showed **Not Calibrated** for background. Nevertheless, native extraction had already written
`background.ini` with `result=true`, plus `Background.csv`, `background.sds` and an updated
`Calibration.sda`. That flag therefore does not establish passage of the wizard's quality checks.
With the vendor application and capture process closed, these outputs were preserved as failed
evidence and removed from the active profile. `Calibration.sda` and `peaks.ini` were restored
from the verified pre-run ROI backup; the vendor's system-configuration file was retained.
The first unit's configuration was unchanged.

Final captured status was inactive, the block mechanism was lowered, and the last sample/block/
cover readings were **59.8/60.0/90.4 °C**; these are not a later confirmation that the plate cooled.
Full failed outputs, the EDS experiment, capture, screenshots, restoration record and diagnostic
analysis remain outside Git in `abi7500-research/captures/abi7500-unit2-background-001/`.
No repeat, optical calibration or cleaning was performed. A separately authorized plate-rotation
comparison is the next diagnostic step before proceeding with calibration.

## Second-unit replacement water plate (2026-10-15)

The operator chose a fresh plate instead of the proposed rotation comparison and explicitly
authorized one repeat background session. The new plate contained **20 µL water per well**, had
an optical seal, and was confirmed loaded with A1 back-left, the drawer closed and hands clear.
Windows v2.3 ran the same background procedure on **275030206**, camera **0000000747**, from
**09:57:40 to 10:08:11 EDT**. The thermal run completed, but the wizard again reported **Failed**.

| Vendor filter | Threshold (FSU) | Previous plate: failing wells | Fresh plate: failing wells |
| --- | --- | --- | --- |
| A | 72,000 | 29 | 44 |
| B | 72,000 | B12, D9 | D9, F3 |
| C | 72,000 | B12, D9 | D9 |
| D | 72,000 | B12, D9 | B12, D9 |
| E | 90,000 | A9, D9 | D9 |

PLR parsed all 960 background coefficients and independently reproduced the complete vendor
threshold lists. At the diagnostic 1,000 ms exposure, D9 remained **741,000–857,000 FSU** across
the five filters. A9 fell below every threshold; B12 decreased by approximately **47–58%** across
the filters but still failed A and D. The filter-A median increased from approximately
**62,200 to 70,345 FSU**. Replacing the plate changed some artifacts without resolving D9 or
producing usable background calibration.

Raw-frame inspection found **30–51% of D9-region pixels clipped at 4095 even at 500 ms**, depending
on filter. Similar diagnostic FSU values across plates therefore do not establish precise signal
stability. The initial 256 ms frame at commanded filter position zero had no clipped D9 pixels.
Persistence across two plates supports investigating a contribution associated with the
instrument position, but does not identify the responsible component or exclude a shared
plate/preparation source. No plate rotation was performed in this session.

All **226 camera transfers** were complete and had distinct hashes. The capture contained
**45,351 matched USB exchanges** and **493 paired thermal exchanges**, with no incomplete
transfers, USB completion errors or missing native-log counterparts. All **865** backup and
experiment files passed SHA-256 checks. ROI masks and the first unit's configuration were
unchanged.

The wizard was canceled without saving failed results; background remained **Not Calibrated**.
Native extraction again wrote `background.ini` with `result=true` despite the wizard failure.
With the vendor and capture processes closed, the failed outputs were archived separately and
the verified ROI-only `Calibration.sda` and `peaks.ini` restored. Final captured status was
inactive with the block mechanism lowered; the last sample/block/cover readings were
**60.0/59.8/89.8 °C**, not a subsequent confirmation of cooling. No additional run or cleaning
was performed. Full evidence and the per-well comparison remain outside Git in
`abi7500-research/captures/abi7500-unit2-background-002/`.

## Second-unit empty, uncovered plate diagnostic (2026-10-15)

The operator authorized one background scan with an **empty, dry, uncovered 96-well plate**,
A1 back-left, drawer closed and hands clear. Windows v2.3 ran the same procedure on
**275030206**, camera **0000000747**, from **10:49:14 to 10:59:42 EDT**. This was a diagnostic
comparison, not an attempt to establish operational background calibration using an empty plate.
The thermal run completed, but the wizard reported **Failed**.

| Vendor filter | Threshold (FSU) | Fresh water plate: failing wells | Empty plate: failing wells | Empty plate D9 (FSU) |
| --- | --- | --- | --- | --- |
| A | 72,000 | 44 | 14 | 766,658 |
| B | 72,000 | D9, F3 | D9 | 483,082 |
| C | 72,000 | D9 | D9 | 506,285 |
| D | 72,000 | B12, D9 | D9 | 626,183 |
| E | 90,000 | D9 | D9 | 460,423 |

Filter A failed at **A2, A12, B1, B12, C1, C2, D1, D9, E1, F4, G6, H5, H6 and H12**.
Its median background decreased from approximately **70,345 to 55,125 FSU**. PLR parsed all
960 coefficients and reproduced every vendor threshold list at the diagnostic 1,000 ms exposure.
Native extraction reported warning **200**, with outlier removal and compensation at **D9**.
Raw D9 pixels remained clipped at 500 ms in filter A (approximately **29%**); filters B–E had
no clipped D9 pixels at 500 ms. Saturation and the compensation warning limit quantitative
interpretation of the fitted coefficients.

Removing water and the seal reduced background but did not resolve D9. Historical operator
notes also record failures of dry, uncovered plates from two batches in June 2025, with an
empty-tray pass, and a later background failure that was accepted through warnings in February
2026. Those notes do not establish the instrument serial. Neither the notes nor this scan
identify plate material alone as the cause or exclude the holder, block or optical path.

All **226 camera transfers** were complete and had distinct hashes. The capture contained
**45,340 matched USB exchanges** and **488 paired thermal exchanges**, with no incomplete
transfers, USB completion errors or missing native-log counterparts. All **867** backup and
experiment files passed SHA-256 checks. ROI masks and the first unit's configuration were
unchanged. A temporary interruption of the file-monitoring bridge was recovered without
replaying completed jobs; the Windows capture and vendor run continued.

The wizard was canceled without **Save Results**; background remained **Not Calibrated**.
Native `result=true` again did not indicate a wizard pass. Diagnostic outputs were preserved
separately, and the verified ROI-only `Calibration.sda` and `peaks.ini` were restored after the
vendor and capture processes closed. Final captured status was inactive with the block
mechanism lowered; the last sample/block/cover readings were **60.0/59.8/89.8 °C**, not a later
confirmation of cooling. No additional scan, cleaning or calibration was performed. Full
evidence and the three-plate comparison remain outside Git in
`abi7500-research/captures/abi7500-unit2-background-003/`.

## Second-unit no-plate diagnostic (2026-10-15)

The operator removed the plate, confirmed clear access and authorized one background scan.
No holder removal was reported. Windows v2.3 ran the same background procedure on
**275030206**, camera **0000000747**, from **11:26:48 to 11:37:17 EDT**. Image collection
finished at 11:34:03. The thermal run completed, but the wizard reported **Failed**.
Well labels below identify the corresponding image regions; no plate was present.

| Vendor filter | Threshold (FSU) | Empty plate: failing regions | No plate: failing regions | No-plate D9 (FSU) | No-plate B12 (FSU) |
| --- | --- | --- | --- | --- | --- |
| A | 72,000 | 14 | 24 | 866,600 | 519,489 |
| B | 72,000 | D9 | B12, D9 | 754,418 | 240,188 |
| C | 72,000 | D9 | B12, D9 | 776,657 | 216,136 |
| D | 72,000 | D9 | B12, D9 | 827,428 | 266,125 |
| E | 90,000 | D9 | B12, D9 | 743,416 | 126,101 |

Filter A failed at **A5, A6, A7, A12, B1, B10, B12, C1, C2, C3, D1, D9, E1, E6, E11,
F1, F4, F11, G11, H2, H3, H6, H8 and H12**. PLR parsed all 960 coefficients and reproduced
every vendor threshold list at the diagnostic 1,000 ms exposure. Native extraction reported
warning **200**, with outlier removal and compensation at **B12**. D9-region raw pixels
clipped at 500 ms in every filter, approximately **11–50%**, limiting quantitative comparisons
of the fitted fluorescence values.

D9 persists without the plate, so the removed plate, water and optical seal cannot be its sole
source. This supports an instrument-associated contribution but does not identify contamination,
holder/block reflections, the optical path or another specific component. Changes between the
plate conditions do not establish that one consumable brand is responsible or that cleaning
any particular component will resolve the signal.

All **226 camera transfers** were complete and had distinct hashes. The capture contained
**45,495 matched USB exchanges** and **487 paired thermal exchanges**, with no incomplete
transfers, USB completion errors or missing native-log counterparts. All **869** backup and
experiment files passed SHA-256 checks. ROI masks and the first unit's configuration were
unchanged.

The wizard was canceled without **Save Results**; background remained **Not Calibrated**.
Native `result=true` again did not indicate a wizard pass. Diagnostic outputs were preserved
separately, and the verified ROI-only `Calibration.sda` and `peaks.ini` were restored after the
vendor and capture processes closed. Final captured status was inactive with the block
mechanism lowered; the last sample/block/cover readings were **60.0/59.8/90.6 °C**, not a later
confirmation of cooling. No additional scan, cleaning or calibration was performed. Full
evidence and the four-condition comparison remain outside Git in
`abi7500-research/captures/abi7500-unit2-background-004/`.

## Second-unit post-cleaning no-plate diagnostic (2026-10-15)

The operator reported cleaning completed, confirmed a dry block, latched cover, closed doors
and clear hands, and authorized one repeat background diagnostic. The plate and holder remained
removed from the maintenance setup. Windows v2.3 ran **275030206**, camera **0000000747**,
from **13:53:50 to 14:04:17 EDT**; image collection finished at 14:01:02. The thermal run
completed, but the background wizard reported **Failed**. Labels below identify image regions
in the empty block, not samples.

| Vendor filter | D9 before (FSU) | D9 after (FSU) | B12 after (FSU) | H5 after (FSU) | Failing regions after |
| --- | --- | --- | --- | --- | --- |
| A | 866,600 | 31,759 | 617,480 | 814,098 | 49 |
| B | 754,418 | 4,498 | 232,436 | 551,925 | 2 |
| C | 776,657 | 2,951 | 180,355 | 440,470 | 2 |
| D | 827,428 | 1,989 | 205,763 | 432,138 | 2 |
| E | 743,416 | 763 | 72,888 | 133,569 | 1 |

**D9 is below the vendor threshold in all five filters.** Across sixteen matched 500 ms frames
per filter, its mean raw pixel intensity fell from approximately 1,874–2,368 to 150–201 counts.
No D9 pixels clipped after cleaning; previously approximately 11–50% clipped. Saturation in
the earlier data limits quantitative interpretation of the fitted FSU reduction.

B12 remains above threshold in filters A–D. H5 is now above threshold in all five filters;
its filter-A region clips at 500 ms, while the other four filters do not. Native warning 200
reports outlier removal and compensation at B12 and H5. Filter A has 49 failing regions.
PLR parsed all 960 background coefficients and reproduced every vendor threshold list.

The earlier no-plate session did not confirm holder removal, while this session continued the
setup with the holder removed. The comparison therefore cannot isolate cleaning from holder
removal or establish why H5 became bright. A scan with the holder restored and a correctly
prepared background plate is needed to assess operational calibration. No further run was
performed under this authorization.

All **226 camera transfers**, **45,396 USB exchanges** and **488 thermal exchanges** were
complete, with no USB completion errors, incomplete replies or missing native-log counterparts.
All **874** backed-up files passed SHA-256 checks. ROI masks and the first unit's configuration
were unchanged. An orphan native process initially changed a log during backup; it was stopped
and the stable replacement backup verified before capture and the run began.

The wizard was canceled without Save Results. Diagnostic outputs were quarantined and the
verified ROI-only calibration restored; the three active configuration-file hashes match the
pre-run values. All vendor processes, capture and the temporary bridge were closed. The last
captured status was inactive, with the mechanism lowered and sample/block/cover readings of
**60.0/59.9/90.4 °C**; this is not a later cooling confirmation. Full evidence and raw-image
comparisons remain outside Git in `abi7500-research/captures/abi7500-unit2-background-005/`.

## Second-unit repeat after further block cleaning (2026-10-15)

The operator completed another cleaning, confirmed a dry block with plate and holder removed,
latched cover, closed doors and clear hands, and authorized one repeat scan. Windows v2.3 ran
**275030206**, camera **0000000747**, from **16:01:20 to 16:11:48 EDT**. Image collection
finished at 16:08:33. The thermal run completed; the background wizard reported **Failed**.
Both this scan and the preceding comparison scan used no plate or holder and identical ROI masks.

**B12, H5 and D9 are below threshold in all five filters.** Filter A has 37 failing regions,
down from 49; filters B–E have none. The native outlier-compensation warning (200) identifies H2.
Selected filter-A background values, evaluated as native offset plus 1000 ms times slope:

| Image region | Previous scan (FSU) | After further cleaning (FSU) | Threshold (FSU) |
| --- | --- | --- | --- |
| B12 | 617,480 | 25,140 | 72,000 |
| H5 | 814,098 | 41,665 | 72,000 |
| D9 | 31,759 | 36,066 | 72,000 |
| H2 | 79,441 | 357,436 | 72,000 |
| C6 | 52,061 | 83,744 | 72,000 |

Matched 500 ms raw images confirm the B12/H5 reduction. Their filter-A mean pixel values fell
from approximately 1,665 to 180 and 2,540 to 203 counts, respectively. D9 remained near
181–186 counts. H2 rose from 278 to 773 and C6 from 222 to 270 counts. None of these five
selected regions clipped in any filter in this scan. The data establish changes in signal;
they do not identify the material responsible or establish that contamination moved between wells.
The empty-block result does not substitute for calibration with a background plate.

All **226 camera transfers**, **45,394 USB exchanges**, and **486 thermal exchanges** were
complete, with no transport errors, incomplete images or missing native-log counterparts.
All **878** backed-up files passed SHA-256 checks. PLR parsed all 960 background coefficients
and reproduced the vendor's full threshold lists. ROI masks and the first unit's configuration
were unchanged.

The wizard was canceled without Save Results. Diagnostic output was preserved separately;
the three active configuration-file hashes match the pre-run ROI-only profile. All vendor
processes, capture and the temporary bridge were closed. The last captured state was inactive
with the block lowered (`0000`) and sample/block/cover temperatures of **60.0/59.9/90.5 °C**;
this is not a later cooling confirmation. Evidence, raw-frame comparisons and per-well values
remain outside Git in `abi7500-research/captures/abi7500-unit2-background-006/`.

## Second-unit water-plate background after cleaning (2026-10-15)

The operator loaded 20 µL water per well and confirmed the holder installed, optical seal,
A1 at the back-left, closed drawer, clear hands and USB connected to Windows. One background
scan was explicitly authorized. Whether the plate was new or reused was not specified.
Windows v2.3 ran **275030206**, camera **0000000747**, from **16:29:23 to 16:39:52 EDT**;
image collection finished at 16:36:39. The thermal run completed, but the background wizard
reported **Failed**: **32 wells exceeded the filter-A threshold; filters B–E had none**.
Native warning 200 identifies outlier removal and compensation at F3.

Selected background values use native offset plus 1000 ms times slope. Both scans use the
same ROI masks, but the reference has no plate or holder; plate, water, seal and holder
changed together. Their separate contributions cannot be identified from this comparison.

| Region | Empty block, filter A (FSU) | Water plate, filter A (FSU) | Threshold (FSU) |
| --- | --- | --- | --- |
| D9 | 36,066 | 35,779 | 72,000 |
| B12 | 25,140 | 31,679 | 72,000 |
| C6 | 83,744 | 72,548 | 72,000 |
| F3 | 78,355 | 275,488 | 72,000 |
| H2 | 357,436 | 150,724 | 72,000 |
| H5 | 41,665 | 113,005 | 72,000 |

**D9 and B12 remain below threshold in all five filters.** C6 exceeds the filter-A limit by
approximately **0.76%**. F3 is the highest filter-A well. Matched 500 ms raw images support
the signal changes: mean filter-A counts at F3 rose from 262 to 573, H5 rose from 203 to 312,
H2 fell from 773 to 396, and C6 fell from 270 to 253. D9 remained near 186 counts. None of
these six selected regions clipped in any filter. These observations do not identify the
source of the remaining background or establish that it originates in the plate alone.

All **226 camera transfers**, **45,292 USB exchanges** and **484 thermal exchanges** were
complete, with no transport errors, incomplete images or missing native-log counterparts.
All **879** backed-up files passed SHA-256 checks. PLR parsed all 960 background coefficients
and reproduced every vendor threshold list. ROI masks and the first unit's configuration
were unchanged.

The wizard was canceled without Save Results. Failed outputs were preserved separately and
the pre-run ROI-only calibration restored; all three active configuration-file hashes match
the pre-run values. Vendor processes, capture and the temporary bridge were closed. The final
captured state was inactive with the block lowered (`0000`) and sample/block/cover readings
of **60.0/59.8/90.5 °C**, not a later cooling confirmation. Evidence and per-well comparisons
remain outside Git in `abi7500-research/captures/abi7500-unit2-background-007/`.

## Second-unit empty-block diagnostic after further cleaning (2026-10-16)

The operator reported cleaning completed, with no plate or holder installed, confirmed a dry
block and closed, latched cover, and authorized one background scan through Windows.
Windows v2.3 ran **275030206**, camera **0000000747**, from **07:08:08 to 07:18:36 EDT**;
image collection finished at 07:15:20. The thermal run completed. The vendor reported
**Failed**, with **46 filter-A regions above 72,000 FSU** and no failures in filters B–E.
No native outlier-compensation warning was recorded in this run's response log.

The comparison is with the October 15 empty-block session `abi7500-unit2-background-006`,
which also had no plate or holder and used identical ROI masks. Region labels describe the
empty block, not samples. Values are native offset plus 1000 ms times slope.

| Region | Previous empty block, filter A (FSU) | Current empty block, filter A (FSU) |
| --- | --- | --- |
| D9 | 36,066 | 39,272 |
| C6 | 83,744 | 80,066 |
| H2 | 357,436 | 102,215 |
| H5 | 41,665 | 69,582 |
| F3 | 78,355 | 82,503 |
| B3 | 68,370 | 287,684 |
| G1 | 60,468 | 239,956 |
| G3 | 115,680 | 395,321 |

**D9, B12 and H5 are below threshold in all five filters.** H2 decreased approximately 71%
in filter A but remains above threshold. C6 is approximately 11.2% above the filter-A limit.
G3 is the largest filter-A peak; B3 and G1 also increased substantially. Filter-A failures
increased from 37 to 46, with median background increasing from approximately 64,595 to
69,984 FSU. The observed changes do not establish a particular contaminant, its movement,
or an optical fault. An empty-block scan does not substitute for background-plate calibration.

Matched 500 ms raw images support the H2 decrease (mean pixel counts 773 to 315), while C6
remains similar (270 to 265), as does D9 (186 to 190). The comparison retains all raw frames
and region measurements outside Git.

All **226 camera transfers**, **45,332 USB exchanges**, and **489 thermal exchanges** were
complete, with no transport errors, incomplete images or missing native-log counterparts.
All **881** backed-up files passed SHA-256 checks. PLR parsed all 960 background coefficients
and reproduced every vendor threshold list. ROI masks and the first unit's configuration
were unchanged.

The wizard was canceled without Save Results. Diagnostic output was preserved separately
and the pre-run ROI-only profile restored; all three active configuration hashes match
their pre-run values. Vendor processes, capture and the temporary bridge were closed. The
last captured state was inactive with the block lowered (`0000`) and sample/block/cover
temperatures of **60.0/59.9/90.5 °C**, not a later cooling confirmation. Full evidence remains
outside Git in `abi7500-research/captures/abi7500-unit2-background-008/`.

## Second-unit rubber-covered plate diagnostic (2026-10-16)

The operator loaded the holder and plate with 1 mm black rubber on top, confirmed the
mechanism clear, and explicitly authorized one scan. Windows v2.3 ran **275030206**, camera
**0000000747**, from **08:03:09 to 08:13:37 EDT**. Image collection finished at 08:10:22.
The vendor reported **Passed**, with a possible-contamination warning and native warning
200: outlier removal and compensation at **C8**.

Compared with the preceding empty-block session `abi7500-unit2-background-008`, using
identical ROI masks, mean filter-A background decreased **87.25%**, from **87,755 to
11,192 FSU**. The number of regions above 72,000 FSU fell from **46 to zero**. Filters B–E
also had no threshold failures. Values use native offset plus 1000 ms times slope.

| Region | Empty block, filter A (FSU) | Covered plate, filter A (FSU) |
| --- | --- | --- |
| C6 | 80,066 | 10,919 |
| D9 | 39,272 | 8,608 |
| B3 | 287,684 | 12,443 |
| G3 | 395,321 | 13,579 |
| C8 | 89,759 | 48,257 |

Matched 500 ms raw images confirm suppression of most earlier bright regions. C6 mean
counts fell from 265 to 156, and G3 from 870 to 161. A small bright point near C8 remains:
all sixteen filter-A frames contain saturated pixels there, totaling 17 pixel observations.
No other well region clipped at 500 ms. The vendor's compensated C8 value must therefore
be distinguished from the uncorrected camera measurements.

This blocked condition supports a substantial contribution from the sample/block optical
path. It does not establish intrinsic block autofluorescence or exclude an optical contribution.
The rubber is uncharacterized, and the plate and holder were also added relative to the
reference; their separate effects cannot be resolved by this pair of scans.

All **226 camera transfers**, **45,394 USB exchanges**, and **488 thermal exchanges** were
complete, with no transport errors or missing native-log counterparts. All **883** backed-up
files passed SHA-256 checks. The vendor automatically wrote the passing diagnostic result;
it was preserved separately and the pre-run ROI-only profile restored. All three active
configuration hashes match their pre-run values. Vendor processes, capture, and the temporary
bridge were closed. The final captured state was inactive with the block lowered (`0000`)
and sample/block/cover temperatures of **60.0/59.8/89.6 °C**, not a later cooling confirmation.
Evidence and comparisons remain outside Git in
`abi7500-research/captures/abi7500-unit2-background-009/`.

## Second-unit empty block after rubber removal and rub-down (2026-10-16)

The operator removed the rubber, plate and holder, reported rubbing down the block, and
explicitly requested another scan. Windows v2.3 ran **275030206**, camera **0000000747**, from
**08:36:44 to 08:47:14 EDT**; image collection finished at 08:43:59. The thermal run completed,
but the vendor reported **Failed**. No native outlier-compensation warning was recorded.

Compared with the earlier empty-block session `abi7500-unit2-background-008`, mean filter-A
background decreased **4.53%** (87,755 to 83,781 FSU), and median background decreased from
69,984 to 53,811 FSU. Filter-A failures decreased from **46 to 31**, but failures appeared in
the other filters: **B3 and E6 in B; G8 in C and D; C9 in E**. The masks were identical.
Values below use native offset plus 1000 ms times slope.

| Region | Filter | Earlier empty block (FSU) | After rub-down (FSU) | Limit (FSU) |
| --- | --- | --- | --- | --- |
| C6 | A | 80,066 | 48,811 | 72,000 |
| C8 | A | 89,759 | 82,419 | 72,000 |
| D9 | A | 39,272 | 32,948 | 72,000 |
| B3 | A | 287,684 | 507,400 | 72,000 |
| E6 | A | 125,041 | 467,054 | 72,000 |
| G8 | C | 5,684 | 118,129 | 72,000 |
| G8 | D | 4,859 | 218,145 | 72,000 |
| C9 | E | 1,521 | 324,973 | 90,000 |

C6 decreased approximately **39%** and is below all five thresholds. Matched 500 ms raw
images support the changes: filter-A mean counts at C6 fell from 265 to 216, while B3 rose
from 610 to 1,125 and E6 from 324 to 942. Filter-E counts at C9 rose from 152 to 751.
No well-region pixels clipped at 500 ms. The tiny saturated C8 point seen in the rubber
session is absent: current filter-A C8 frame maxima are 431–442 counts, versus 4,095 with
rubber. Its broader regional background still exceeds the filter-A limit.

All **226 camera transfers**, **45,351 USB exchanges**, and **492 thermal exchanges** were
complete, with no transport errors or missing native-log counterparts. All **885** backed-up
files passed SHA-256 checks, and PLR reproduced every vendor threshold list. Diagnostic
output was preserved separately; all three active configuration hashes match the pre-run
ROI-only profile. Vendor processes, capture, and the temporary bridge were closed. The last
captured state was inactive with the block lowered (`0000`) and sample/block/cover readings
of **60.0/59.9/90.6 °C**, not a later cooling confirmation.

Both empty-block scans exclude the plate and holder. A rubber-covered plate scan occurred
between them; the rub-down material and technique were not specified. The changing spatial
pattern does not identify a contaminant, establish intrinsic block autofluorescence, or
validate operational background calibration. Evidence and the three-condition comparison
remain outside Git in `abi7500-research/captures/abi7500-unit2-background-010/`.

## Optical-calibration reconstruction from recorded images (2026-10-19, offline)

The PLR calculation was checked against `abi7500-unit2-optical-001`, a vendor v2.3 optical
calibration on instrument **275030206**, camera **0000000747**, with a uniform mixed-dye plate.
The vendor reported **Passed** with **H5** automatically corrected. The capture contains 110
complete frames: 45 startup/exposure-search images and 65 final images used for calculation.
The matching ROI masks and native `Background.csv` were preserved alongside the acquisition.

`calculate_optical_calibration()` independently reconstructed all **480 integer spectra** from
those 65 original full frames, selected **H5** through spectral-outlier detection, substituted
its five unflagged adjacent wells, and reproduced all **480 saved coefficient strings** and
all **480 reloaded binary32 factors** exactly. Expected vendor factors were comparison targets,
not calculation inputs. Original spectra remain available before the H5 substitution.
The generated file uses explicit instrument/camera identities and source hashes; its bytes
therefore differ from the vendor file even though its coefficient arrays match.

This was an offline replay with no USB traffic or vendor runtime. It validates coefficient
calculation for the captured path, not a fresh Ubuntu-controlled optical-calibration session.
Exposure search, calibration thermal/cover settings and the full acquisition sequence still
require integration and hardware validation. Existing measurement/run warnings remain applicable.
See [calculation usage and acceptance checks](optical-calibration.md).

The replay report, generated `uniformity.ini`, input manifest and binary-source provenance are
outside Git in `abi7500-research/captures/abi7500-unit2-optical-001/analysis/plr-optical-calibration/`.
The reference vendor uniformity SHA-256 is
`c19c0f5b3d331ab70cfb9105fac1fa55e4ebee37c6e42c09eabd7f1d8695639a`.

## ROX pure-dye reconstruction from recorded images (2026-10-19, offline)

The vendor reference `abi7500-unit2-dye-rox-001` used instrument **275030206**, camera
**0000000747**, a ROX-only plate and 60 °C acquisition. It completed and saved a passing
calibration. The USB capture contains 101 complete frames; the final 65 use physical-filter
exposures **1800/720/1719/1800/558 ms** and counts **1/16/16/16/16**. These are settings for
this plate, not general calibration defaults. Matching ROI masks and background were retained.

`calculate_pure_dye_calibration()` processes those original images without expected spectra
as inputs. All **480 saved binary32 values** match the vendor `puredye.ini` exactly. The native
iterative screening reports **G1 and B10**, matching the log, while the Java saved-calibration
path makes a single pass and replaces **G1 only**. Its neighbor average uses binary64 sums of
normalized binary32 values. B10 remains unchanged in the saved Java file; native diagnostic
messages therefore must not be treated as a list of substitutions in that file.

G1 clipped in saved channel 1. The primary ROX channel 4 did not clip. PLR retains clipping
flags and both correction decisions, preserves original spectra, and rejects serialization
when a clipped well is left unreplaced. The emitted interchange file has PLR provenance and
explicit serials; its bytes differ from the vendor file despite identical spectrum values.
No calibration files were installed on the instrument or Windows PC by this replay.

The report and generated interchange file are outside Git in
`abi7500-research/captures/abi7500-unit2-dye-rox-001/analysis/plr-pure-dye-calibration/`.
The reference vendor `puredye.ini` SHA-256 is
`62bf38d4098d43657c9021f10b364c181eadf8b41e5483e8fa45cb99993235c6`.
See [pure-dye processing and limits](pure-dye-calibration.md). This offline result does not
validate automatic exposure selection, fresh Ubuntu calibration acquisition, other dyes,
multicomponent analysis or PCR assay performance.

## Fresh Ubuntu ROX calibration acquisition (2026-10-19)

One explicitly approved session, `abi7500-plr-unit2-dye-rox-001`, used PLR directly over USB
on instrument **275030206**, camera **0000000747** (C9254-02/G2.10). The operator retained the
same sealed ROX-only plate, A1 at the back-left, and moved USB from Windows to Ubuntu.
No vendor runtime was used for acquisition or calculation.

The Standard-mode 20 µL method held **60 °C for 300 seconds**, then **30 °C for 30 seconds**.
After one minute of stabilization, PLR collected all **65 images** using the vendor reference's
physical-filter exposures **1800/720/1719/1800/558 ms** and counts **1/16/16/16/16**. Collection
and wheel homing consumed 174 reported seconds, leaving **65 seconds** in the acquisition hold.
Every image retained its complete 157,312-byte transfer, verified crop and thermal/optical
metadata. All 2,014 thermal command/reply pairs passed the byte audit. There was exactly one
method upload, Start, home and plate release, with no acquisition retries, lamp-counter commands
or exposure-table writes. Eight earlier journal records and the first unit's journal were unchanged.

The run completed normally with matching instrument history. Cleanup confirmed filter 0, lamp
off, plate unloaded and USB closed. Final sample/block readings were **30.0/29.9 °C**; the
heated cover remained **104.9 °C**. Closing USB does not cool that cover.

The same ROI masks and background produced 480 relative-spectrum values. Both recovered
screening paths selected the same wells as the vendor reference: native screening flagged
**G1 and B10**, while the Java-style saved calculation replaced **G1 only**. The numerical
spectral checks passed. However, the main ROX channel (physical filter 2, saved channel 4)
had unreplaced saturation flags at **A10 and B10**. PLR's `to_ini()` rejected export with
`Unreplaced saturated wells require exposure review`; no accepted calibration file was emitted
or installed. G1's clipping in saved channel 1 and its replacement remain separately recorded.

Compared with the earlier vendor acquisition, the background-corrected main ROX signal was
a median **14.4% higher**. Across the 480 corrected, peak-normalized values, RMS difference was
**0.00674** and maximum absolute difference **0.07762**. The main channel is normalized to 1
by construction; its identical normalized values are not evidence of equal absolute brightness.
All wells are included in these statistics, including the flagged regions. The comparison
report also supplies separately labeled statistics excluding flagged wells.

During image acquisition, sample temperature was **60.0 °C**, block **59.9–60.0 °C**, and cover
**104.4–105.5 °C**. The vendor calibration used approximately 90 °C cover temperature. Different
cover settings, acquisition times and illumination histories prevent attributing the brightness
change to one cause. This validates fixed-exposure acquisition and the saturation export guard,
not an accepted fresh calibration, automatic exposure selection, matching vendor thermal/cover
behavior, other dyes or PCR assay performance.

Original frames, transport, source snapshots, approval, journal copies, input calibrations,
independent saturation counts and the comparison figure are outside Git in
`abi7500-research/captures/abi7500-plr-unit2-dye-rox-001/`.

## Exposure-search and cover-control preparation (offline)

`BudgieManagerEXT.dll`'s `OptimizeExposureTable` (RVA `0x1315f`) and `OptimizeExpHelper`
(`0x13509`) establish the integer search and per-well saturation decision. Reprocessing all
35 original vendor ROX probe images with their saved masks reproduces every requested duration
and the five final settings **1800/720/1719/1800/558 ms**. Compact per-well count fixtures retain
source-frame and mask hashes. Synthetic USB tests separately exercise the eleven-probe path,
separation of probes from final images, and interrupted-acquisition recovery boundaries.

The captured cover-set request is command `0x35`, payload `005a0001`: 90 integer degrees with
native mode 1. `TC9700Control::GetCoverTemp` establishes command `0x36` and its six-byte reply;
that query was subsequently checked in the failed session below. PLR retains its final word without assigning
measurement units, and uses the established `0x29` status for measured cover temperature.
Tests cover exact framing, matching-state write omission, readback mismatch, lost acknowledgement,
duplicate-start prevention, and refusal to collect with an unverified cover setting.

This is software validation only. It does not establish that the new acquisition path avoids
saturation on the loaded plate or that cover temperature explains the earlier brightness change.

## Cover verification stopped the automatic ROX scan (2026-10-19)

The authorized `abi7500-plr-unit2-dye-rox-002` session used the same ROX plate on Fast Dx
275030206/C9254-02. Command `0x36` initially returned `006900010068`: target 105, mode 1,
raw measurement 104. PLR sent one standard Start (`0x13`), then one acknowledged cover request
(`0x35`, `005a0001`). Immediate readback remained `006900010069`, reporting a 105 °C target.
The controller rejected verification and cancelled the run before any camera exposure.

The transport audit validated all 91 thermal exchanges: one method upload, one Start, one cover
write, one cancellation and no `CJ`/`CC`/`CD` commands. Cleanup confirmed inactive status,
lamp off, filter 0 and USB closed. Final sample/block readings were 26.0/25.2 °C; cover was
104.1 °C. The plate was not released. All nine previous run records and the first unit's journal
were unchanged. No calibration result was generated.

Review of the vendor capture found a sequencing discrepancy: it sets the cover before
preparation/upload, then starts with **`0x7E`**, not `0x13`. The native binary names this command
`TC9700Control::StartMethodNoCoverSetpoint` (RVA `0x37ee7`). PLR's explicit-cover path now follows
that order, verifies configuration before and after Start, and journals the exact Start command.
Tests prevent submission after a pre-Start mismatch and prevent retries or fallback after an
ambiguous `0x7E` acknowledgement. The standard PCR Start path remains `0x13`.

The correction has offline tests and vendor capture evidence; the following session checks it
on hardware. A delayed cover-register update or other firmware behavior was not established
by this short failed attempt. Evidence is outside Git in
`abi7500-research/captures/abi7500-plr-unit2-dye-rox-002/`.


## Automatic ROX acquisition with a 90 °C cover (2026-10-19)

The explicitly authorized retry, `abi7500-plr-unit2-dye-rox-003`, completed on Fast Dx
275030206/C9254-02/G2.10, USB serial `0000000747`, with the same sealed ROX plate. PLR set
90 °C/mode 1 before preparation/upload and verified it before and after one cover-preserving
Start (`0x7E`). No standard Start (`0x13`) or fallback was sent. The program held 60 °C for
600 seconds, then 30 °C for 30 seconds; collection began after 120 seconds at 60 °C.

The five saturation searches used **35 probe images** and selected **1800/594/1593/1800/486 ms**
in physical filter order. All **65 final images** completed in counts **1/16/16/16/16**.
The complete search, final acquisition and one wheel home consumed 342 reported seconds,
leaving 137 seconds of the 60 °C hold. Final-image boundary readings were sample **60.0 °C**,
block **59.9–60.0 °C**, and cover **89.3–90.5 °C**. The higher initial cover temperature was
allowed to settle before the acquisition window.

An independent audit validated **2,236 thermal request/reply pairs**, the cover/start ordering,
all 100 original camera transfers against exported byte hashes, the camera crops, per-well
probe counts, selected exposures, hold identities and optical status. The run completed normally,
with one plate release, lamp off, filter 0 and USB closed. Final sample/block readings were
30.0/29.9 °C; the cover remained hot at 90.4 °C. All ten previous run records and the first
instrument's journal were unchanged. No firmware, lamp-counter, exposure-table or installed
vendor calibration was changed.

No well in any final frame exceeded the native threshold of more than six ROI pixels above
4000. In particular, A10 and B10 no longer blocked export. Native screening and the Java-style
calculation both flagged **G1**; its neighbor correction remains recorded. The spectral checks
and clipping guard passed, and `analysis/plr-rox.ini` contains the newly calculated 480 relative
spectral values. This is a PLR interchange result, not vendor certification or an installed
calibration.

Compared with the earlier vendor run, the background- and exposure-corrected main ROX signal
was a median **24.2% higher**. Across all 480 corrected normalized values, RMS difference was
**0.01896**, maximum absolute difference **0.26400**, and median absolute difference **0.00125**.
The maximum difference is at B10 in saved channel 1 (physical filter 5). Excluding G1 and B10,
which were flagged by either run, RMS difference was **0.01451**. These exclusions are reported
separately; they do not remove those wells from the saved results. The dominant ROX channel is
normalized to 1 by construction and is not an independent agreement check.

The corrected start and automatic exposure acquisition are hardware-checked for this single
session. Matching the cover setting did not eliminate the brightness/spectral discrepancy.
Different acquisition times, illumination histories and selected exposures prevent assigning
one cause. Repeatability, other dyes, spectral unmixing and PCR assay performance remain
unverified. Evidence, reference inputs, the generated INI, independent saturation counts and
comparison figure are outside Git in
`abi7500-research/captures/abi7500-plr-unit2-dye-rox-003/`.


## Immediate PLR ROX repeatability check (2026-10-19)

The separately authorized `abi7500-plr-unit2-dye-rox-004` repeated the successful automatic
ROX scan on Fast Dx 275030206/C9254-02/G2.10 with the same plate, source code, thermal program,
collection plan, masks and background. The initial cover was already near 90 °C, whereas the
preceding run began near 105 °C and settled before collection. Each run warmed the lamp for
30 seconds and waited 120 seconds at 60 °C before searching exposures. Both used one
cover-preserving Start, then cooled and released the plate normally.

The repeat used **25 probes** and **65 final frames**, selecting
**1800/612/1800/1800/504 ms** in physical filter order. Filter 3 required only one probe:
B10 had 48 ROI pixels above 4000 in the preceding run's 2000 ms initial probe, but none in
this repeat's corresponding probe. Both final collections had no saturated wells. Acquisition
and home consumed 311 reported seconds, leaving 169 seconds of the hold. Final-image sample
readings were 60.0 °C and cover readings 89.3–90.6 °C.

The independent transport audit passed **2,341 thermal exchanges** and all **90 image transfers**.
There was one Start, one wheel home and one plate release. All eleven earlier run records and
the first instrument's journal remained unchanged. Cleanup confirmed lamp off, filter 0,
USB closed, sample/block 30.0/29.9 °C and cover 90.6 °C. The ROX calculation passed again,
retaining the G1 neighbor correction; its 480 exported binary32 values reloaded exactly.
No generated calibration was installed in the vendor software.

This pair does **not** establish quantitative repeatability:

- The background- and exposure-corrected main ROX signal was a median **5.02% lower** in the
  repeat. Every well decreased, from **1.64% to 10.23%**; E9 had the largest decrease.
  The 95th percentile of absolute per-well change was 8.60%.
- Across all 480 corrected normalized values, RMS difference was **0.01283** and median absolute
  difference **0.00055**. The largest changes were B10 (**−0.20331**) and C12 (**−0.15851**)
  in saved channel 1, followed by B5. These wells were not removed from the comparison.
  G1, the only corrected/flagged well in either PLR calculation, is separately identified.
- Within the repeat's 16 main-channel images, the per-well median change between the mean of
  the last four and first four ring-subtracted ROI signals was **+0.957%**. In the preceding
  run it was **+0.009%**, although some central wells declined (E6: **−4.33%**).
  Median temporal CV was 0.381% and 0.192%, respectively. These statistics include drift and
  are not pure camera-noise estimates or independent repeatability replicates.
- Relative to the earlier vendor acquisition, the repeat's median main-channel signal remained
  **18.55% higher**; the preceding PLR run was 24.16% higher. The vendor comparison therefore
  remains unresolved, alongside the observed variation between PLR scans.

Only two same-plate PLR runs were compared. Automatic exposures, initial thermal conditions,
illumination history and acquisition timing differed, so this experiment does not identify the
cause of the changes. The dominant ROX channel is normalized to 1 and does not independently
validate intensity agreement. Exposure search is working and clipping is controlled, while
signal repeatability and matched vendor agreement need further controlled validation.

Original transport, camera data, source snapshots, approval, journal backups, calibration exports,
probe comparisons, paired heatmaps and within-collection signal plots are outside Git in
`abi7500-research/captures/abi7500-plr-unit2-dye-rox-004/`. The original reference archives remain
unchanged; this archive records their hashes and includes the comparison input reports.

## Fixed-exposure ROX stability and journal contention (2026-10-19)

The authorized `abi7500-plr-unit2-dye-rox-005` used the same sealed ROX plate and
Fast Dx 275030206/C9254-02/G2.10. Its single Start scheduled three consecutive 60 °C/360-second
holds followed by 30 °C/30 seconds. Each collection began after 120 seconds of its hold, with
a 240-second read window. Physical filters 1–5 used fixed **1800/612/1800/1800/504 ms** exposures
and **1/16/16/16/16** images. Lamp power 1 remained on, with filter 0 closing the light path
between sets. Homing was planned only after the third collection.

**The session did not complete.** A local SQLite lock interrupted the third set's filter 5.
There are **181 complete images**, one reserved but unstarted acquisition and thirteen
unattempted image identities. The first two collections are complete; the third contains all
images for filters 1–4 and only two of sixteen for filter 5. That incomplete filter group is
excluded from comparisons and no five-filter calibration is calculated from the third set.

The independent audit validated **3,476 thermal exchanges**, all 181 original image transfers,
their reconstructed crops and the exact fixed exposure requests/echoes. It counted **182 CJ**
settings but only **181 CC/CD** acquisitions: the final exposure was configured but never
started. There was one Start, one cancellation sequence, no wheel home and no plate release.
There were no saturated wells in any complete frame. All twelve previous run records and the
first instrument's journal were unchanged.

All **three 16-image main ROX groups** are complete, so their stability comparison remains valid:

| Comparison | Median change in ring-subtracted ROI signal | Range across 96 wells |
| --- | ---: | ---: |
| Set 2 versus set 1 | +7.98% | +3.34% to +9.75% |
| Set 3 versus set 1 | +8.97% | +3.46% to +11.63% |
| Set 3 versus set 2 | +0.92% | −0.16% to +2.01% |

Using the same native background and exposure correction gives median changes of +8.06% and
+9.05% for sets 2 and 3 relative to set 1. No wells are discarded or neighbor-corrected in the
intensity comparisons. Within each ROX burst, the median last-four/first-four change was
**+0.871%, −0.111% and −0.274%**; median temporal CV was **0.348%, 0.080% and 0.127%**.
The two complete five-filter calculations passed their numerical checks and flagged G1 for
correction. No calibration was installed or exported for use.

These data demonstrate intensity variation even with identical exposures and no intervening
home command. The increase became smaller between the later sets, but this does not establish
stability or identify lamp warmup, plate equilibration or another cause. A longer settling-period
comparison remains a separate, approval-gated experiment. The incomplete full session is not
marked as a hardware-validation pass.

The database exception occurred at the same time as a separate read-only progress query.
An offline regression reproduced the failure: the checkpoint connection's zero-second SQLite
timeout caused an immediate error when committing against a short-lived reader. Checkpoint
writes now wait up to five seconds; the separate exclusive controller lock still fails
immediately. The regression and all **263 driver tests** passed. This database wait does not
replay an exposure or Start. At the end of this session it had only been tested offline;
the following session exercises the updated driver on hardware without deliberately adding locks.
Future session monitoring should use the runner's event log rather than external database scans.

Cleanup cancelled the owned run, parked filter 0, turned the lamp off and closed USB. The plate
was **not released**. Last recorded sample/block/cover temperatures were **55.4/49.2/89.3 °C**;
no later cooling measurement is claimed. Original transport, journal snapshots, complete and
uncertain acquisition records, per-well values, comparison figures and offline fix evidence are
retained outside Git in `abi7500-research/captures/abi7500-plr-unit2-dye-rox-005/`.

## Longer lamp and thermal settling before fixed ROX collections (2026-10-19)

The separately authorized `abi7500-plr-unit2-dye-rox-006` completed on the same sealed ROX
plate and Fast Dx 275030206/C9254-02/G2.10. It added a **780-second hold at 60 °C without
images**, followed by the same three 60 °C/360-second collection holds and 30 °C/30-second
cooldown. Each collection began after 120 seconds of its hold, giving **15 minutes of
programmed settling before collection 1**. The cover remained at 90 °C, lamp power 1 stayed
on, and filter 0 closed the light path before and between collections. Fixed physical-filter
exposures and counts remained **1800/612/1800/1800/504 ms** and **1/16/16/16/16**.

All **195 images** completed with no exposure-search probes or saturated wells. The independent
audit verified **7,370 thermal exchanges**, exact exposure requests and echoes, all original
image transfers and crops, one cover-preserving Start, one wheel home after collection 3,
one plate release, and no cancellation. Collection margins were **32, 29 and 23 seconds**.
All thirteen earlier run records and the first instrument's journal remained unchanged.

The recorded counters show **901 seconds** between the initial holding-phase boundary and
collection 1, and **966.29 seconds** of lamp-on time before its first image. The controller's
sample estimate initially approached 60 °C from 59.2 °C; it was 60.0 °C throughout the final
minute before collection and at every image boundary. During imaging, measured block and
cover ranges were **59.9–60.0 °C** and **89.3–90.6 °C**. These are controller readings, not an
independent measurement inside every well.

The longer-settling session had substantially less main-channel drift. Comparisons use all
96 wells, each run's own first-set mean, sixteen images per set and the unchanged 612 ms
physical-filter-2 exposure. The earlier interrupted session retained all 48 main ROX images,
so these comparisons do not use its incomplete filter-5 group.

| Main ROX comparison | Earlier 2-minute settling: median change | 15-minute settling: median change | Longer-settling range across wells |
| --- | ---: | ---: | ---: |
| Set 2 versus set 1 | +7.98% | +0.073% | −0.732% to +1.107% |
| Set 3 versus set 1 | +8.97% | −0.198% | −1.855% to +1.116% |
| Set 3 versus set 2 | +0.92% | −0.278% | −1.131% to +0.064% |

For set 3 versus set 1, the 95th percentile of absolute per-well change was **0.907%**.
Native background/exposure correction gave median changes of **+0.073% and −0.200%** for
sets 2 and 3. Within the three main ROX bursts, median last-four/first-four changes were
**−0.294%, −0.299% and −0.333%**; median temporal CV was **0.134%, 0.130% and 0.143%**.
These CVs include drift rather than estimating camera noise alone.

Residual change remains: physical filters 3, 4 and 5 declined by median **1.01%, 2.00% and
1.60%** by set 3. All three five-filter calculations passed the recovered numerical checks,
with G1 corrected each time. Corrected normalized-spectrum RMS differences versus set 1
were **0.000951 and 0.001739**, with maximum absolute differences **0.004531 and 0.008003**.
No calibration interchange file was generated or installed.

This is one session per settling condition on the same plate, in a fixed order. Longer lamp
warmup and longer thermal equilibration changed together, and prior illumination history
differed. The result supports retaining a longer settling period for further ROX validation;
it does not isolate the cause, establish a universal warmup duration, or resolve vendor
agreement, other dyes or assay performance. The first longer-settling measurement was a
median **8.07% above** the earlier run's first measurement, so normalizing each run separately
must not be mistaken for agreement in absolute signal.

The unchanged driver that passed 263 offline tests completed this session using the
five-second journal checkpoint timeout. Progress monitoring read append-only event logs;
external journal readers and artificial lock contention were not introduced. Eleven runner
scenarios, archived-frame analysis replay, identical-repeat calculation checks and the
documentation build passed without executing hardware notebook cells.

Cleanup confirmed inactive state, sample/block **30.0/29.9 °C**, lamp off, filter 0, plate
released and USB closed; the cover remained **90.1 °C**. Original data, source snapshots,
approval, audit, per-well values, short/long-settling figures and reference hashes are outside
Git in `abi7500-research/captures/abi7500-plr-unit2-dye-rox-006/`.

## Fresh vendor ROX reference after the settling study (2026-10-20)

The separately authorized `abi7500-unit2-dye-rox-002` used v2.3 on Windows, the same
sealed ROX plate, and Fast Dx 275030206/C9254-02/G2.10. The vendor result was **Passed**
and saved automatically as Current. Only `puredye.ini` and `puredye.sds` changed in
this instrument's configuration; ROI masks, background, optical coefficients and the
first instrument's configuration remained byte-identical. Before/after backups and
the Windows-to-Ubuntu transfer were checked with SHA-256.

This used the **unmodified vendor dye routine**, not the proposed 15-minute settling
condition. The wizard did not expose that setting; the deviation was disclosed before
Start, and no template or binary was changed. The captured program was one
**60 °C/312-second hold**, with a 90 °C cover setting and one cover-preserving Start.
The first exposure-search image followed Start by **205.54 seconds**, including cover
warmup and ramping. Final collection began **48.89 seconds** later, approximately
**113.05 seconds after the first observed holding status**. These intervals are not
equivalent to 900 seconds of settling at 60 °C.

The capture contained **90 complete images**: one startup image, 24 exposure-search
images and 65 final images. Physical-filter exposures were
**1800/702/1800/1800/729 ms**, with **1/16/16/16/16** final images. No final image
clipped. All **20,932 USB request/completion pairs** and **599 thermal exchanges**
were complete, without errors; every thermal request and reply matched the vendor
logs. No lamp-timer reset was sent.

PLR recalculated **all 480 saved binary32 coefficients exactly** from the 65 captured
final frames and unchanged background/ROI inputs. Both the native extraction log
and PLR identified **G1** for correction. The same offline calculation first reproduced
all 480 values from the earlier vendor capture. The reconstructed PLR interchange
file was retained as evidence and was not installed.

Acquisition agreement remains unresolved. Across all 96 wells, today's main ROX
background/exposure-corrected signal was a median **2.05% above** the earlier vendor
scan, ranging from −10.14% to +11.30%. It was a median **17.17% below** the first
15-minute-settled PLR collection, ranging from −22.71% to −10.38%. Different settling,
exposure search and illumination histories prevent interpreting this as a controlled
vendor/PLR agreement test. Exact recalculation establishes processing agreement for
the same images, not equivalence of separate acquisitions.

The last captured status was inactive, with the plate mechanism released, sample/block
**60.0/59.8 °C**, and cover **90.6 °C**. The final lamp commands selected vendor standby
(enabled 1, power 0), not lamp off. Vendor software, USBPcap and the temporary bridge
were closed. Captures, original frames, coefficients, comparisons and backups remain
outside Git in `abi7500-research/captures/abi7500-unit2-dye-rox-002/`.

## Captured startup and ROX acquisition in PLR (2026-10-20)

The separately authorized `abi7500-plr-unit2-dye-rox-007` completed on Ubuntu with the same
sealed ROX plate and Fast Dx 275030206/C9254-02/G2.10. PLR reproduced the captured main
startup order: method upload, lamp power 1, exposure-table read, plate load, wheel home and
cover-preserving Start. Before startup, the lamp spent 270 seconds enabled at standby
power 0 behind filter 0. The method used the captured **30 µL controller setting**,
**60 °C/312-second hold**, 90 °C cover, and 252-second collection window. Actual plate
volume remained 20 µL per well. A separate 30 °C/30-second cooldown followed acquisition.

All **25 exposure-search probes and 65 final images** completed. Selected physical-filter
exposures were **1800/684/1800/1800/747 ms**, with **1/16/16/16/16** final images. Windows
reference `abi7500-unit2-dye-rox-002` selected 1800/702/1800/1800/729 ms using 24 probes.
PLR parked at absolute filter 0 after all searches, then collected filters 1–5 and homed.
The independent audit verified **1,511 thermal exchanges**, exact exposure requests,
all original image transfers and crops, one method upload, one Start, two homes and one
plate release. There was no cancellation, lamp-timer command or exposure-table write.
Collection and final homing consumed **236 seconds**, leaving **16 seconds** in the hold.
All fourteen earlier run records and the first instrument's journal remained unchanged.
Repeated retrieval of the completed collection generated no USB traffic.

No final image clipped. The recovered calculation passed and identified **G1** for correction,
as Windows did. Comparison used identical ROI masks, background and frame counts:

| Comparison against the fresh Windows reference | Result |
| --- | ---: |
| Main ROX signal, median across 96 wells | +3.10% |
| Main ROX signal, range across wells | −2.83% to +7.55% |
| Main ROX signal, median excluding flagged G1 | +3.10% |
| RMS difference across 480 corrected normalized coefficients | 0.001696 |
| Maximum absolute normalized coefficient difference | 0.014692 |

Main ROX signal here means background/exposure-corrected values before each well is
normalized to its strongest channel. The normalized coefficient differences are absolute
values, not relative percentages. Replaying the same Windows frames through the updated
code still reproduced all 480 vendor binary32 coefficients exactly.

Relative to this same Windows reference, the earlier automatic PLR session `004` was a
median **15.94% higher**, and the first 15-minute-settled collection in session `006` was
**20.72% higher**. The smaller discrepancy in `007` supports using the captured startup and
method for further comparison. These sessions also differed in settling, exposure and
illumination history; the difference cannot be attributed to a single command change.

Timing was not identical. Immediately before Start, the cover was **89.9 °C in PLR**
versus **59.2 °C in Windows**. Using camera-start (`CC`) requests, the first final image
began **166.79 versus 260.58 seconds after lamp power 1**, and **131.38 versus 111.41
seconds after the first observed holding status**. PLR's search took 68.11 seconds versus
49.08 seconds, including the transition to final collection. Its additional ownership,
temperature and optics checks remained enabled.

Within the main ROX burst, median last-four/first-four signal increased **0.946% in PLR**
and **0.789% in Windows**. First-to-last exposure starts spanned **27.25 and 16.26 seconds**,
respectively; these are not equal-duration drift comparisons. At PLR final-image boundaries,
sample estimates were 60.0 °C, block readings 59.9–60.0 °C and cover readings 89.4–90.6 °C.
One acquisition per condition does not establish repeatability, calibration equivalence,
other-dye performance or assay validity.

The frozen driver passed **271 offline tests**, 23 final targeted tests, lint, formatting,
scoped type checks and the documentation build. Eleven runner failure scenarios passed.
Monitoring used append-only logs without external journal access during the run. Cleanup
confirmed inactive state, sample/block **30.0/29.9 °C**, lamp off, filter 0, plate released
and USB closed. The cover remained **90.3 °C**. The generated ROX interchange file was
retained offline; no vendor calibration or firmware was installed. Original images, complete
transport, journals, source snapshots, comparison plots and approval remain outside Git in
`abi7500-research/captures/abi7500-plr-unit2-dye-rox-007/`.

## Interrupted repeat of the captured ROX sequence (2026-10-20)

The separately authorized `abi7500-plr-unit2-dye-rox-008` attempted the identical method,
collection plan, 270-second standby and runtime driver on the same ROX plate. The instrument
started inactive at sample/block **25.0/24.9 °C**, cover **89.4 °C**, filter 0 and lamp off.
The preceding run had initially found vendor lamp standby. This repeat therefore matched
the prescribed startup, while the lamp's earlier history still differed.

The run stopped during the physical-filter-2 exposure search. **Eight complete 157,312-byte
image transfers** were preserved: seven acquisitions completed their checks, and the eighth
remained **uncertain**. Its 724 ms exposure and image download finished, but the subsequent
filter-position query, command **0x6B, sequence 540**, received no valid serial reply.
The bridge returned **455 zero-valid-byte reads** over 4.999 seconds before the five-second
command deadline expired. Padding bytes were excluded from the reply. No USB transfer error
was recorded; the capture does not establish why the controller reply was missing.

The full image was saved before the failed post-exposure observations. It was not accepted
as a completed acquisition, and neither the exposure nor its command was repeated. None
of the **65 planned final images** was attempted. No repeatability result or new calibration
can be calculated from this incomplete exposure search.

The independent audit verified **597 completed thermal exchanges and one unanswered
query**, all eight image downloads and crops, and unchanged records for all fifteen prior
runs. The first instrument's journal also remained unchanged. Cleanup reconnected once,
verified identity and ownership, cancelled the run, set the cancellation cooldown target
to 25 °C, parked filter 0, turned the lamp off and closed USB. There was **one Start, one
cancellation, one startup home and no plate release**. The last observed state was inactive
at sample/block **57.9/51.9 °C**, cover **89.4 °C**; no later temperature is asserted.

A small captured-query fixture and an offline regression exercise timeout after a full
download. The regression checks retained pixels, missing final observations, an uncertain
record and zero further exposure traffic when the same identity is requested, including
after reopening its journal. All **16 acquisition tests** passed. Production communication
behavior and the five-second timeout were unchanged; this failure does not justify a blind
retry or an unsupported timing change.

The session is not a hardware-validation pass. Full transport, original images, journal
snapshots, frozen execution sources, approval, failure audit and regression evidence remain
outside Git in `abi7500-research/captures/abi7500-plr-unit2-dye-rox-008/`. A further hardware
attempt requires separate approval.

## Completed repeat of the captured ROX sequence (2026-10-20)

The separately approved retry `abi7500-plr-unit2-dye-rox-009` completed on the same
sealed ROX plate in Fast Dx 275030206. Its production runtime, thermal program,
collection plan, ROI masks and background matched successful session `007`.
The only source difference was the offline regression test added after interrupted
session `008`; communication behavior and the five-second deadline were unchanged.

All **25 probes and 65 final images** completed, with physical-filter exposures
**1800/639/1800/1800/711 ms** and no clipped final pixels. The independent audit
verified all image transfers and crops, **1,511 thermal exchanges**, one Start,
two homes, one release and no cancellation. Collection and final homing consumed
**235 seconds**, leaving **17 seconds** in the hold. All sixteen earlier run records
and the first instrument's journal were unchanged. Cached retrieval sent no USB traffic.

The recovered pure-dye calculation passed and again corrected **G1**. Results use
background/exposure-corrected main ROX signal before spectral normalization:

| Comparison | Median across 96 wells | Range across wells |
| --- | ---: | ---: |
| Repeat `009` relative to PLR `007` | +5.46% | +1.96% to +8.83% |
| Repeat `009` relative to Windows `002` | +8.78% | +4.71% to +13.63% |

Excluding flagged G1 changed the PLR-repeat median to +5.46%. The RMS difference
across 480 corrected normalized coefficients was **0.002444** against PLR `007`
and **0.002860** against Windows; maximum absolute differences were **0.012484**
and **0.018283**, respectively. These are absolute coefficient differences.

Within the 16-image main ROX burst, the median last-four/first-four change was
**+0.544% over 27.08 seconds** between first and last exposure starts. Previous PLR
was +0.946% over 27.25 seconds; Windows was +0.789% over 16.26 seconds. Final-image
sample readings were 60.0 °C, block readings 59.9–60.0 °C and cover readings
89.3–90.6 °C. The first final image began 159.82 seconds after lamp power 1,
versus 166.79 seconds in `007`. This retry found the plate already loaded after
the cancelled attempt, so startup did not need another load command.

This pair verifies another execution of the sequence and measures its variation;
it does not establish calibration equivalence. Every well's corrected main ROX
signal increased between successful PLR runs despite the smaller within-burst drift.
Exposure selection and earlier illumination history differed, and an interrupted
search occurred between the two completed runs. These data do not identify the
cause of the signal change or establish general repeatability.

Cleanup confirmed inactivity, sample/block **30.0/29.8 °C**, filter 0, lamp off,
plate released and USB closed. The cover remained **89.5 °C**. No calibration was
installed. Original images, complete transport, frozen sources, journal snapshots,
approval, offline coefficients and comparisons remain outside Git in
`abi7500-research/captures/abi7500-plr-unit2-dye-rox-009/`.

## Settled ROX exposure-response diagnostic (2026-10-20)

The separately authorized `abi7500-plr-unit2-dye-rox-010` completed on the same
sealed ROX plate in Fast Dx 275030206. Production code was unchanged. The run
used the captured calibration startup, 270-second lamp standby, 30 µL controller
setting and 90 °C cover. After a 60 °C/870-second settling hold, eight 60 °C/60-second
holds collected in their last 30 seconds. This provided a nominal 900 seconds of
thermal settling before the first images. The actual plate volume remained 20 µL.

Each collection used the existing three-exposure bracket mode at physical filter 2,
with preset **300/450/600 ms** exposures. All **24 images** completed, with no search
probes, saturated wells or clipped pixels; the maximum pixel value was 3741.
The lamp remained at power 1, with absolute filter-0 parking after each bracket.
Only the final bracket homed the wheel. A 30 °C/30-second cooldown followed.

The independent audit verified **6,810 thermal exchanges**, all exposure requests
and returned durations, original image transfers and crops, one Start, two homes,
one release and no cancellation. Reported settling before collection was **902 seconds**.
The lamp had spent approximately **940 seconds at power 1** before the first acquisition.
Each three-image collection and parking took 9–10 reported seconds, leaving at least
19 seconds in its hold. All seventeen previous run records and the first instrument's
journal remained unchanged. Cached collection retrieval sent no USB traffic.

Analysis retained all 96 wells, including previously flagged G1. Each frame used the
same saved ROI mask and native background as Windows reference `002`. The public
PLR per-frame calculation was checked against independent arithmetic for background
offsets, slopes and effective integration time. Values precede uniformity correction,
neighbor replacement and spectral peak normalization.

| Measurement across 96 wells | Median | Range across wells |
| --- | ---: | ---: |
| Corrected 300 ms signal relative to 600 ms | +0.103% | −0.081% to +0.443% |
| Corrected 450 ms signal relative to 600 ms | +0.115% | +0.013% to +0.348% |
| Last-two/first-two bracket change, 300 ms | −0.172% | −1.154% to +0.449% |
| Last-two/first-two bracket change, 450 ms | −0.171% | −1.125% to +0.431% |
| Last-two/first-two bracket change, 600 ms | −0.181% | −1.083% to +0.388% |

Exposure-response comparisons use each well's mean across eight frames. The complete
series spanned **429.98 seconds** between first and last camera-start requests.
Median temporal CV was 0.127%, 0.112% and 0.106% at 300, 450 and 600 ms, respectively;
these include drift. The mean raw 450 ms response differed from linear interpolation
between 300 and 600 ms by a median +0.082%. Excluding G1 barely changed the median
exposure-response results.

The 600 ms corrected signal nevertheless remained a median **8.78% above PLR `009`**,
**14.80% above PLR `007`**, and **18.25% above Windows `002`**. These separate runs
differed in settling, prior illumination, selected exposure and averaging count.
The small response difference within the tested 300–600 ms range does not explain
the much larger cross-run shift. It supports investigating acquisition history,
while neither identifying lamp versus thermal effects nor testing the earlier
639–702 ms exposures directly. Ascending exposure order also leaves short-term
within-bracket drift as a confounder.

This is a single-channel diagnostic, not a five-filter calibration. It supplies
within-session stability and exposure-response evidence without establishing
between-run agreement or assay validity. No calibration file was installed.
Cleanup confirmed inactivity, sample/block **30.0/29.9 °C**, filter 0, lamp off,
plate released and USB closed; the cover remained **90.6 °C**. Complete evidence
and comparison figures remain outside Git in
`abi7500-research/captures/abi7500-plr-unit2-dye-rox-010/`.

## Settled Windows ROX reference and native background precision (2026-10-20)

The separately authorized `abi7500-unit2-rox-settled-001` completed one normal Windows v2.3
experiment on Fast Dx 275030206, C9254-02/G2.10, using the same sealed ROX plate with 20 µL
per well. The controller method specified 30 µL, a 60 °C/900-second non-collecting hold,
eight 60 °C/60-second collecting holds, and a 30 °C/30-second cooldown. This reference
used normal Start `0x13`. Although the saved XML cover setting was 90 °C, observed cover
temperature during collection was approximately 105 °C; the runtime behavior is the reference.

USB capture retained **136 complete images**: one startup image, fifteen pre-run images,
and **120 scheduled images**. Each scheduled point collected three stored exposures per
physical filter: 540/180/60 ms for filters 1 and 2, 900/300/100 ms for filter 3,
765/255/85 ms for filter 4, and 270/90/30 ms for filter 5. The independent audit found
one thermal Start, no abort, no exposure-table writes, 2,022 thermal exchanges and 21,471
paired USB transfers without unmatched or truncated records. All 21 backed-up calibration
files and all 21 mask files were unchanged.

The first scheduled camera start occurred 942.34 seconds after the first observed 60 °C hold
and 1,091.99 seconds after lamp power 1. The vendor homed the filter wheel after each
collection, unlike PLR `010`, which parked at absolute position 0 between brackets and
homed only after the final bracket. Each vendor fifteen-image bracket spanned 18.66–19.31
seconds between first and last camera starts. Status queries preceded the whole bracket;
the associated status is not a fresh temperature measurement for every image.

Offline replay independently checked **11,520 well/ring extractions**. With the native
`Calibration.sda` background coefficients, PLR matched **3,840/3,840 saved EDS integers**
and **3,840/3,840 corrected binary32 values** calculated by the original vendor Java code
from those saved integers. The version-5 CSV background export rounded three slopes enough
to lose one count: cycle 3/filter 3/G10, cycle 5/filter 1/H4 and cycle 6/filter 1/G10
(physical filter numbering). The bounded SDA reader preserves those binary32 coefficients;
regression fixtures retain the background section and three small original image patches.
No calibration was generated or installed.

All 96 wells were positive and unsaturated in the main ROX channel, physical filter 2.
At 540 ms, the median last-two/first-two change was **−0.546%**, ranging from −1.347% to
+0.240%; median temporal CV was **0.233%**. Mean exposure-normalized signals at 180 and
60 ms differed from 540 ms by medians −0.019% and −0.240%, respectively.

The vendor 540 ms mean was a median **1.123% above PLR `010` at 600 ms**, ranging from
−1.900% to +3.631% across wells. Both were processed with the same full-precision
background; archived PLR `010` raw counts were reprocessed without changing that session's
original files. These separate acquisitions differed in cover temperature, startup,
wheel homing, exposure order and illumination history. This comparison does not identify
the cause of the remaining shift, establish calibration equivalence, or validate a PCR assay.

One release command was verified, followed by an unloaded plate state. The last observed
status was inactive with sample/block **30.0/29.9 °C** and cover **105.5 °C**. The lamp
was enabled at standby power 0, not switched off. The vendor application and USB capture
closed normally. Full recordings, original EDS, unchanged calibration backups, source
snapshots and comparison figures remain outside Git in
`abi7500-research/captures/abi7500-unit2-rox-settled-001/`.

## Normal-start five-filter ROX comparison (2026-10-20)

The separately authorized `abi7500-plr-unit2-dye-rox-011` completed directly from Ubuntu on
Fast Dx 275030206, C9254-02/G2.10, using the same sealed ROX plate as the settled Windows
reference. Actual volume remained 20 µL per well, with A1 at the back-left. The captured
controller method specified 30 µL: 60 °C/900 seconds, eight 60 °C/60-second collection holds,
then 30 °C/30 seconds.

The public `standby_startup=True` path used 270 seconds of lamp standby behind filter 0,
then upload, lamp power 1, exposure-table read, plate load, wheel home and normal Start `0x13`.
No cover override was sent. During image acquisition, sample readings were 60.0 °C,
block readings 59.9–60.0 °C and cover readings 104.4–105.5 °C.

All **120 planned images** completed: three descending stored exposures through each of
five physical filters at eight collection points, with home after every collection.
The exposures matched the Windows reference: 540/180/60 ms for filters 1 and 2,
900/300/100 ms for filter 3, 765/255/85 ms for filter 4 and 270/90/30 ms for filter 5.
There were no exposure-search probes, saturated wells or clipped pixels; the maximum
image pixel value was 3435.

An independent transport audit verified **7,063 thermal exchanges**, original image
transfers and crops, exposure commands and returned durations, one Start, nine homes,
one release and no cancellation. No calibration or exposure table was written.
Each collection, including homing and PLR's status checks, took 34–35 reported seconds
and finished with **20 seconds remaining** in its hold. Cached result retrieval generated
no USB traffic. All 18 previous run records, 1,039 acquisition records and 81 collection
records remained byte-for-byte unchanged, as did the first instrument's separate journal.
The new standby intent used journal format 6.

Processing used the Windows reference's full-precision `Calibration.sda`, five original
ROI masks and EDS uniformity coefficients. Independent arithmetic checked **11,520**
well/ring extractions and exposure/background corrections, plus **3,840** binary32
uniformity corrections. These checks concern the new PLR images. The earlier
3,840/3,840 saved-vendor agreement remains a separate same-image replay result;
independent acquisitions are not expected to produce identical integers.

All 96 ROX wells were positive and unsaturated and were retained. At physical filter 2,
per-well means across eight collections gave:

| Exposure | Median PLR/Windows difference | Range across wells | Median last-two/first-two change | Median temporal CV |
| --- | ---: | ---: | ---: | ---: |
| 540 ms | −1.212% | −3.740% to +1.914% | −0.622% | 0.264% |
| 180 ms | −1.249% | −3.788% to +1.862% | −0.606% | 0.272% |
| 60 ms | −1.286% | −3.807% to +2.072% | −0.574% | 0.352% |

Exposure-normalized means at 180 and 60 ms differed from 540 ms by medians −0.046%
and −0.275%, respectively. The Windows reference's 540 ms decline was −0.546%.
The cross-run differences show spatial structure rather than a uniform multiplier.
Off-peak channels retain all raw values and explicit exclusions for ratio summaries;
their weak, background-subtracted signals do not support the same interpretation
as the main ROX channel.

The first PLR camera start occurred 909.82 seconds after the first observed settling
hold, versus 942.34 seconds in Windows. PLR began its collection window with 55 seconds
remaining; the vendor used approximately 22–23 seconds and queried status before the
whole bracket rather than around every image. The lamp power 1 request preceded PLR's first
camera start by 945.47 seconds, versus 1,091.99 seconds in Windows. Windows also
recorded pre-run images, and prior illumination history differed. Matching the normal
Start, cover behavior, exposure order and homing therefore does not establish identical
acquisition conditions, calibration equivalence or the cause of the remaining variation.

Cleanup confirmed inactivity, sample/block **30.0/29.8 °C**, filter 0, lamp off,
plate released and USB closed. The cover remained **105.4 °C**. No calibration was
installed and no PCR assay, spectral unmixing, Rn or Ct calculation was validated.
Full transport, original frames, frozen sources, journal snapshots, processing inputs,
per-well CSV files and comparison figures remain outside Git in
`abi7500-research/captures/abi7500-plr-unit2-dye-rox-011/`.
