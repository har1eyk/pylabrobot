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
| Thermal-run start/cancellation | Not tested | Not implemented |
| Live image acquisition and well fluorescence | Not tested | Not implemented |

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
