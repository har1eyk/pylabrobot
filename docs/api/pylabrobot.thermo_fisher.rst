.. currentmodule:: pylabrobot.thermo_fisher

pylabrobot.thermo_fisher package
================================

ABI 7500 Fast Dx
----------------

Experimental USB connection, identity and status queries, plus offline protocol and data tools.
Connection/status, a completed 35 °C/60-second hold and one start/cancel were checked on G2.10; see
:doc:`/user_guide/thermo_fisher/abi7500_fast_dx/hardware-validation` for the tested scope.
Experimental thermal/optical run control requires a persistent journal. Wider thermal programs, startup
cancellation and recovery after communication faults remain hardware unverified.
``get_plate_status()`` reports the internal mechanism state; ``release_plate(run_id)`` requires a
freshly verified finished run, lamp off and stationary filter 0. Its durable intent prevents command
replay after an uncertain response. See the plate-release section in the thermal-run guide.
Direct Ubuntu sessions verified illuminated snapshots at filter positions 1 and 5 and lamp
shutdown. Offline camera processing reproduces all 960 vendor well integers and corrected values
from one recorded acquisition. Thirty images were collected within two scheduled holds and the
thermal program completed normally. Optional collection-end parking keeps the lamp enabled and
verifies stationary position 0; explicit collection ordinals can request native homing instead.
Optical fault recovery remains unverified;
current calibration validity is not established. See
:doc:`/user_guide/thermo_fisher/abi7500_fast_dx/thermal-runs` for limits and recovery behavior.
``get_collection_records(run_id)`` reads saved optical progress without USB traffic or loading
camera payloads; ``get_collections(run_id)`` retrieves the original complete or partial images.
``export_run(run_id, path, background=..., uniformity=..., masks=...)`` packages a completed
optical run as a ZIP containing original images, calibration files, per-well CSV tables and
content hashes, without instrument I/O. See the export example in the collection guide below.
Snapshot behavior is documented in :doc:`/user_guide/thermo_fisher/abi7500_fast_dx/raw-snapshots`.
See :doc:`/user_guide/thermo_fisher/abi7500_fast_dx/optics-and-measurements` for optical control,
calibration provenance and measurement limits. The experimental collection scheduler is documented
in :doc:`/user_guide/thermo_fisher/abi7500_fast_dx/synchronized-acquisition`.

.. currentmodule:: pylabrobot.thermo_fisher.abi7500_fast_dx

.. autosummary::
  :toctree: _autosummary
  :nosignatures:

    ABI7500FastDx
    ABI7500DeviceError
    ABI7500Identity
    ABI7500Status
    ABI7500PlateStatus
    ABI7500History
    ABI7500RunError
    ThermalRun
    ThermalStep
    ThermalCycle
    ThermalProgram
    CameraAcquisition
    CameraExposure
    OpticalState
    CollectionFilter
    CollectionPlan
    CollectionPoint
    CollectionRecord
    FluorescenceCollection

.. currentmodule:: pylabrobot.thermo_fisher.abi7500_fast_dx.protocol

.. autosummary::
  :toctree: _autosummary
  :nosignatures:
  :recursive:

    ABI7500ProtocolError
    ThermalPacket
    ThermalPacketDecoder
    encode_serial_write
    encode_serial_read
    decode_serial_read

.. currentmodule:: pylabrobot.thermo_fisher.abi7500_fast_dx.camera

.. autosummary::
  :toctree: _autosummary
  :nosignatures:

    CameraGeometry
    CameraFrame
    CameraFrameAssembler

.. currentmodule:: pylabrobot.thermo_fisher.abi7500_fast_dx.results

.. autosummary::
  :toctree: _autosummary
  :nosignatures:

    SavedFilterData
    read_eds_filter_data

.. currentmodule:: pylabrobot.thermo_fisher.abi7500_fast_dx.calibration

.. autosummary::
  :toctree: _autosummary
  :nosignatures:

    UniformityCalibration
    UniformityCorrectedData
    WellMeasurement
    apply_uniformity
    read_eds_uniformity

.. currentmodule:: pylabrobot.thermo_fisher.abi7500_fast_dx.image_processing

.. autosummary::
  :toctree: _autosummary
  :nosignatures:

    OpticalBlackImage
    PlatePixelMask
    CameraWellData
    WellPixelMeasurement

.. currentmodule:: pylabrobot.thermo_fisher.abi7500_fast_dx.fluorescence

.. autosummary::
  :toctree: _autosummary
  :nosignatures:

    BackgroundCalibration
    ExtractedWell
    FluorescenceFrame
    CameraWellMeasurement
    CameraFilterResult

.. currentmodule:: pylabrobot.thermo_fisher.abi7500_fast_dx.exposure_selection

.. autosummary::
  :toctree: _autosummary
  :nosignatures:

    ExposureSelection

ALPS Heat Sealers
-----------------

.. currentmodule:: pylabrobot.thermo_fisher.alps.sealer

.. autosummary::
  :toctree: _autosummary
  :nosignatures:
  :recursive:

    ThermoScientificALPSSealer
    ThermoScientificALPSError

.. currentmodule:: pylabrobot.thermo_fisher.alps.alps300

.. autosummary::
  :toctree: _autosummary
  :nosignatures:
  :recursive:

    ThermoScientificALPS300
    ALPS300Status

.. currentmodule:: pylabrobot.thermo_fisher.alps.alps3000

.. autosummary::
  :toctree: _autosummary
  :nosignatures:
  :recursive:

    ThermoScientificALPS3000
    ALPS3000Status

.. currentmodule:: pylabrobot.thermo_fisher.alps.alps5000

.. autosummary::
  :toctree: _autosummary
  :nosignatures:
  :recursive:

    ThermoScientificALPS5000
    ALPS5000Status

BTX Gemini X2
-------------

.. currentmodule:: pylabrobot.thermo_fisher.btx.gemini.X2.gemini_x2

.. autosummary::
  :toctree: _autosummary
  :nosignatures:
  :recursive:

    BTXGeminiX2

.. currentmodule:: pylabrobot.thermo_fisher.btx.gemini.X2.standard

.. autosummary::
  :toctree: _autosummary
  :nosignatures:
  :recursive:

    ElectroporationProtocol
    ElectroporationPreparationDetails
    ElectroporationExecutionDetails
    ElectroporationCancellationDetails
    ElectroporationLogCapture
    ElectroporationCleanup
    PreparedElectroporationRun
    ElectroporationRunResult
    ElectroporationCancellationResult

.. currentmodule:: pylabrobot.thermo_fisher.btx.gemini.X2.ht200

.. autosummary::
  :toctree: _autosummary
  :nosignatures:
  :recursive:

    BTXHT200
