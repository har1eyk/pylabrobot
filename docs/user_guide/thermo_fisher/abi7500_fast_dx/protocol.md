# Recovered packet formats

These formats describe the reference software, not a hardware-validated Fast Dx protocol.
The codecs in `pylabrobot.thermo_fisher.abi7500_fast_dx.protocol` operate entirely offline.

## Evidence and limits

Sources include the downloaded **7500 Software v2.3** installer, the reference PC's installation
and logs, and approved USB captures of startup/shutdown, a completed acquisition, and cancellation.
The installer's
[release notes](https://downloads.thermofisher.com/7500/100023991%20-%207500%20and%207500F%20F%20Software%20v2.3_vSubmitted.pdf)
list Windows 10 x64 and 7500/7500 Fast; they do not establish Fast Dx compatibility.
Prefer the original control PC's working software until its version and instrument compatibility
are known.

| Reference | SHA-256 | Relevant entry points (RVA) |
|---|---|---|
| `BudgieManagerEXT.dll` from the main installer CAB | `9dbff7366d38a6c43b3ba363dd007b40f65d0dead3c1d3b9eb09428f91a8102f` | `_buildCommand` `0x370b0`, `_prepareWriteBuffer` `0x36e48`, `checksum` `0x3764b` |
| 77,824-byte `ArpLib.dll` from the main installer CAB | `4d4c5e7db38a9a8a6cefeab214eb1efd721d5a12dd201bbc7bdeabff3b05f93b` | `ARPAPI_SendString` `0x2c70`, `ARPAPI_GetString` `0x2d30`, send helper `0x5930` |

The installer also contains larger Win32/Win64 ArpLib DLLs in its CCD driver directories. Their
behavior is not established by the smaller DLL's analysis. The reference PC's v2.3 installation
contains byte-identical copies of both DLLs listed above. Compare USB captures before relying on
these formats for control; matching installed files does not establish the Windows driver's USB
transfer behavior.

The small `test_data/encoder_vectors.json` fixture contains outputs of the thermal encoder executed
in an isolated x86 emulator. Only memory-copy and memory-fill external calls were emulated; there
was no OS or instrument access. The vectors cover empty, binary, multi-packet, and maximum-size
payloads. They are **binary-derived fixtures, not hardware captures**. Arbitrary payload vectors
test framing and do not represent valid experiment instructions. Installer files, decompilation
outputs, calibration backups, and full captures must remain outside Git.

The reference PC's `config/logs/shell.log` contains 499 complete thermal packets: 249 matching
request/reply pairs and an initial reply whose request precedes the retained log. All 499 satisfy
the length and checksum rules below. Every complete pair echoes its command and sequence, and
every recorded packet uses address `ff ff ff ff` and error field zero. These observations cover
the saved log, not all firmware or error conditions. `test_data/thermal_log_vectors.json` retains
eight unmodified packets from four query/reply pairs, with the full log's hash and source lines.
These are vendor debug-log bytes, not USB captures; the outer USB messages are absent.

The separate `abi7500-connect-001.pcapng` capture has SHA-256
`f78202bd40ec06915d06936a8528644f92bde7d4cd4bf23befedeab6c62e6a35`.
It contains 590 USBPcap records over 119.385 seconds, all for bus 1, device 7. All captured lengths
equal original lengths. The 295 submission/completion pairs are complete, and every completion
reports USB status zero. Drop statistics are absent, so zero packet loss cannot be independently
established. USBPcap submission status fields are not completion results.

The first six records are injected descriptor/configuration metadata at a common timestamp, not
evidence of physical re-enumeration during the session. The remaining records contain 221 bulk
writes and 71 bulk reads. The 55 thermal requests and 55 reconstructed replies match the
corresponding `shell.log` bytes exactly; the log also contains older sessions that must be excluded
from this comparison. `test_data/usb_serial_vectors.json` retains a small selection of unmodified
bulk payloads, including padded empty reads and replies split across multiple serial reads.

## USB and the serial tunnel

The bundled `ArpDrv.inf` associates the Hamamatsu camera with USB VID `0x0c2b`, PID `0x0300`.
An Ubuntu descriptor inventory for the available instrument reported a C9254-01 device, interface
0, bulk OUT endpoint `0x06`, and bulk IN endpoint `0x85`, with 64-byte endpoint packets.
The startup capture confirms these endpoints and VID/PID. Its camera identification response names
`C9254-03`, which differs from the earlier USB product string; retain both identifiers rather than
assuming they are interchangeable model names.

The PC uses the external **USB Type-B port**. The reference library tunnels thermal-controller
serial packets inside Hamamatsu USB messages. This does not make the external connection RS-232
and does not expose a Linux serial port. The
[EZ-Tap Pro](https://stratusengineering.com/product/ez-tap-pro-rs-232-passive-tap-module-bus-analyzer-protocol-analyzer-2/)
monitors RS-232 wiring; its USB connector transfers those captures to a host. Use USB capture for
the 7500's external cable.

| Message | Format | Implemented codec |
|---|---|---|
| Serial write | ASCII `RS`, payload length as four space-padded decimal characters, then binary payload | `encode_serial_write` |
| Serial read request | ASCII `RR`, four-character decimal length including the two-byte reply count | `encode_serial_read` |
| Serial read reply | Little-endian unsigned 16-bit payload count, then serial bytes | `decode_serial_read` |

The serial payload limit is 1024 bytes. For example, sending 21 serial bytes produces `RS  21`
followed by those bytes; requesting 21 serial bytes produces `RR  23`. The write helper divides the
wrapped message into writes of at most 64 bytes, without a terminator or an extra zero-length write.
The wrapper can make a full 1024-byte payload span 1030 bytes.

In the startup capture, all 64 RR replies occupy exactly the requested wire length. The first two
bytes report how many following bytes are valid serial data; the remaining tail can contain
arbitrary nonzero bytes. Five reads have count zero despite nonzero tails. Four version replies
arrive over two RR transactions each: a 91-byte thermal reply splits into 68+23, 70+21, 70+21, or
35+56 valid bytes. The application requests the remaining serial length on the second read.
Each RR reply in this capture is one USB read completion, including those longer than the
endpoint's 64-byte maximum packet size; host transfer records are not individual USB bus packets.

Pass the original serial capacity as `decode_serial_read(data, requested_size=capacity)` to
validate the complete wire length and extract only the counted bytes. Without that argument, the
decoder accepts only an unpadded reply. Feed the extracted bytes into `ThermalPacketDecoder` to
assemble complete thermal packets. Never feed the unused tail into that decoder or interpret a
zero count as a complete thermal response. No command retries or live I/O are implemented.

## Observed startup and shutdown

The capture begins with the vendor login dialog and includes a warning that one or more
calibrations are expired or invalid. The operator reports choosing **Ignore & Continue Startup**.
The log reports a successful connection and later disconnection. This establishes communication
with the reference installation, not valid calibration or validated fluorescence measurements.

Before the first thermal query, the captured bulk exchanges include:

| OUT message | IN response or observed purpose |
|---|---|
| `CH   5` | Five ASCII bytes, `ready` |
| `Cf  15,,.,1.05,2.19,,` | Reference initialization message; no corresponding IN transfer |
| `CF  64` | 64 bytes containing a NUL-terminated camera identification string and an opaque tail |
| `CI   4` | `41 01 f5 00`, two little-endian values: 321 and 245 |
| `RP  109600,8,0,0` | Internal serial configuration, not an external RS-232 connection |
| `RC` | Serial flush, also observed during setup and at application shutdown |

`ARPAPI_GetImageSize` at RVA `0x2320` subtracts one from the first CI value and, in its default
mode, five from the second, yielding 320 by 240. Preserve the original CI values; the complete
transfer and reference crop are described below.

Startup also sends thermal filter-wheel home/move and lamp configuration commands; merely opening
the application is not a passive status query. While idle, the capture contains 88 `C:` writes
at roughly half-second intervals without corresponding bulk IN transfers. The reference library's
background routine at RVA `0x1070` contains that command. Its timing and recovery requirements need
further validation. These observations are protocol evidence, not a script to replay blindly.

## Thermal packets

Multi-byte thermal fields use **big endian**, unlike the outer read count.

| Offset | Size | Meaning |
|---|---|---|
| 0 | 2 | ASCII `XX` |
| 2 | 4 | Address; outbound reference requests use `ff ff ff ff` |
| 6 | 2 | Length excluding the first six bytes: `15 + payload length` |
| 8 | 4 | ASCII `9700` |
| 12 | 2 | Command identifier |
| 14 | 2 | Error field; zero in requests |
| 16 | 4 | Message sequence number |
| 20 | variable | Binary command payload |
| last | 1 | Sum of bytes from offset 6 through the payload, modulo 256 |

Total size is `21 + payload length`; the tunnel limit allows a payload of at most 1003 bytes.
Although the native buffer writer places a `#` after the checksum in memory, that byte is excluded
from the transmitted length. It must not be appended to the encoded packet.
The native function named `crcCheck8` calls an additive checksum, not a polynomial CRC.

A reference temperature query with sequence 1 is:

```text
58 58 ff ff ff ff 00 0f 39 37 30 30 00 06 00 00 00 00 00 01 e6
```

Static inspection also identifies the following command IDs and expected response payload sizes.
These are evidence for capture annotation, not exposed hardware methods:

| ID | Native operation | Expected response payload |
|---|---|---|
| `0x06` | GetTemperature | 16 bytes |
| `0x0a` | GetVersion | 70 bytes |
| `0x17` | GetInstrumentState | 46 bytes |
| `0x36` | GetCoverTemp | 6 bytes |

Units, state values, error meanings, and sequence behavior after failures or reconnects still
require validation. The decoder preserves opaque payloads and errors; it does not treat a valid
checksum as a successful operation or retry any command.

## Acquisition and cancellation captures

Two engineering sessions on the original Windows 10 PC used 7500 Software v2.3 and the existing
sealed plate. The operator confirmed that the plate and seal appeared intact. Expired calibration
warnings were acknowledged; these sessions do not validate an assay or calibration. The original
reference experiment was preserved, and separate templates were used for the test experiments.

| Capture | SHA-256 | USB records / complete pairs | Thermal pairs | Complete camera frames |
|---|---|---|---|---|
| `abi7500-acquire-001.pcap` | `4a76d6b4b604cfffaad61081dd83eb8fb9a64f18a453c5a48f2a3504738671bd` | 15,782 / 7,891 | 643 | 46 |
| `abi7500-cancel-001.pcap` | `ca31a95997b6c8a52b17ea17f701da0eb49de87d79bca8b9d22fc198f77cf118` | 6,962 / 3,481 | 332 | 16 |

Both captures have complete submission/completion pairs, equal captured/original record lengths,
and zero USB completion statuses. Every reconstructed thermal request and reply matches its vendor
log bytes, including command, sequence, payload, and checksum. All thermal error fields are zero.
Drop statistics are unavailable; completeness checks do not independently establish zero packet
loss. These captures started after the application's initial connection and include run-specific
initialization. The separate connection capture supplies the application startup evidence.

The completed program uses Standard mode, 20 µL, a 105 °C cover, an initial 95 °C hold for 15 seconds,
then two cycles of 95 °C for 15 seconds and 60 °C for 60 seconds with collection. The vendor reports
completion at 2026-10-09 10:28:57.231, Windows local time (UTC−04:00).

The cancellation session uses the same program. The vendor logs the stop action at 10:38:28.731
during the initial heating ramp and finishes cancellation at 10:38:40.574, reporting
`onservercommandcomplete(33, -38, "User stopped pending command.")`. This is an application result,
not a thermal packet error code. Capture includes normal application disconnection afterward.

Both sessions contain an identical 98-byte thermal program payload under command `0x12`, followed
by a `0x13` start request. Cancellation adds a `0x07` stop and a `0x22` request with payload `00 fa`.
The native `StopMethod` routine also emits `0x07` and a setpoint call with argument 250. These
observations are evidence for further recovery, not sufficient validation of a general run API.
`test_data/usb_run_vectors.json` retains the exact program/start/stop exchange bytes, including
the two OUT transfer boundaries of the program request. Tests check framing without replaying them.

## Camera transfer and reference crop

The offline `camera` module reconstructs the complete CD payload and the image produced by
`ARPAPI_GetImageData` at RVA `0x2500`. The original USB bytes are retained in `CameraFrame.wire_data`.

| Operation | Captured format |
|---|---|
| Set camera exposure parameters | `CJ   2,  540` (example; preserve both integers pending unit validation) |
| Start capture | `CC` |
| Poll readiness | `CH   5`, replying `ready` when ready |
| Read image | `CD  64,2458 `, including the final space |

For the 321 × 245 wire dimensions, the reference library requests
`2 × (floor(321 × 245 / 64) + 1)` blocks of 64 bytes: 157,312 bytes. This contains 157,290 bytes of
little-endian uint16 samples and a 22-byte uninterpreted tail. Retain that tail without assuming
zero padding. A captured frame consists of 38 reads of 4,096 bytes and one read of 1,664 bytes;
these are host transfers, not individual USB packets. The assembler accepts arbitrary fragments
but rejects a missing or excess byte.

The default reference copy (OPB=0) produces 320 × 240 pixels. For output row `r` starting at zero,
copy 320 samples starting at wire sample index `321 × (r + 5) − 1`. The one-pixel offset matters:
a simple rectangular crop at wire row 5, column 0 is incorrect. OPB=1 produces 320 × 245 pixels
starting each row at wire sample index `321 × r`. Neither copy mode establishes physical well
orientation or applies calibration.

These copy rules were checked by executing the native function in an isolated x86 emulator with
synthetic ramp buffers, then with every captured camera payload. PLR's default image bytes agree
exactly for all 62 frames (4,761,600 pixels). No external functions or hardware were used by the
emulator. `test_data/camera_crop_vectors.json` retains the synthetic native outputs;
`camera_capture_vector.json` retains one compressed, unmodified CD payload, its actual fragment
lengths, and the native output hash. Full captures remain outside Git.

The completed run contains one initial camera frame followed by three groups of 15 frames. In each
group, filter-position requests 1 through 5 precede three exposures apiece. The latter two groups
precede the cycle 1 and cycle 2 result events. The cancelled run contains the initial frame and
the first 15-frame group, with no completed cycle result. Exposure selection/combination and the
meaning of the initialization images still require tracing; do not average the three images or
assign dye names based only on their order.

## Saved calibration and image evidence

The reference installation stores instrument-specific calibration in
`eclipse/plugins/com.apldbio.sds.instrument.sds7500fast_1.0.0/config/<instrument-serial>/`.
That directory includes `background.ini`, `uniformity.ini`, `puredye.ini`, `peaks.ini`,
`Calibration.sda`, `SystemConfiguration.sdc`, and native `.sds` calibration experiments.
The INI-like files include multiline numeric tables delimited by `<<eof` and `eof`; they are not
ordinary single-line INI settings. Embedded copies in `.eds` files are snapshots and may differ
from the installation's currently selected calibration.

The native module's `bin/Mask/` directory contains five `ROIMask_W96_F*_I809_Commit.aoi` files,
alongside intermediate, silent, and failed masks. Preserve those distinctions rather than choosing
whichever file is newest. The masks use an MFC archive format; their geometry is not yet decoded
by PLR.

Five `AutoROI_F0.tif` through `AutoROI_F4.tif` files in that directory are genuine uncompressed
320 by 240 images, stored as unsigned 16-bit samples. Those saved images establish a reference
file layout, not the USB camera frame layout or its complete processing history. Their filter
indices must not be equated to dye names without validating the calibration mapping.

In contrast, the 1,035 `.tiff` members examined in the supplied experiment archive are each 768
bytes: 96 big-endian signed 64-bit well values. The Java `RawFilterData.convertBuffer` implementation
confirms that encoding. These entries are not TIFF camera images despite their suffix. Their names
encode stage, cycle, collection point, and filter. Preserve the original values; correction status
and physical well orientation require validation against the acquisition and calibration pipeline.

The completed engineering EDS contains ten arrays (two cycles × five filters). All 960 integers
match the corresponding `onupdatespectradataxml` event values exactly, using vendor well indices
1 through 96. The cancelled experiment contains no filter arrays. `read_eds_filter_data` reads
these members without extracting the ZIP, preserving their original bytes, integer precision,
acquisition indices, and vendor ordering. It rejects malformed arrays and duplicate acquisition
indices. This agreement validates the saved-data decoder, not the conversion from pixels to wells.

## Evidence needed for control and measurements

Before implementing the standalone `ABI7500FastDx` class:

- Establish initialization and connection teardown using the working software, including camera
  settings and the internal serial configuration. Avoid firmware-download paths.
- Match request/response sequences, state transitions, errors, and timeouts across connection,
  run start, progress, completion, cancellation, and reconnection. Determine how to query an active
  run after a lost acknowledgement. A local run ID alone cannot prevent duplicates after reconnect.
- Extend the verified image transfer and crop with exposure units, exposure selection/combination,
  channel identity, and robust association with thermal collection events. Preserve original bytes
  alongside metadata.
- Recover ROI geometry and calibration processing from the same instrument's files and reference
  outputs. Preserve separate raw and corrected measurements. Do not assume an evenly spaced well
  grid or infer orientation from a plausible image.
- Compare frames and per-well values from the same acquisition against vendor outputs, with
  explicit orientation, channel, cycle, and numerical-tolerance checks. Keep synchronized timing
  on the instrument where required.

Tests establish packet framing, camera transfer reconstruction, the reference image copy, and
saved filter-array decoding. They do not validate live PLR acquisition/cancellation, timeout
recovery, duplicate-run prevention, physical well orientation, or calibrated fluorescence.
