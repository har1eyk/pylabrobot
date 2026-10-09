.. currentmodule:: pylabrobot.thermo_fisher

pylabrobot.thermo_fisher package
================================

ABI 7500 Fast Dx
----------------

Experimental USB connection, identity and status queries, plus offline protocol and data tools.
Connection, identity and status were checked directly from Ubuntu on one G2.10 instrument; see
:doc:`/user_guide/thermo_fisher/abi7500_fast_dx/hardware-validation` for the tested scope.
Run control and live optical acquisition remain unimplemented and unverified.

.. currentmodule:: pylabrobot.thermo_fisher.abi7500_fast_dx

.. autosummary::
  :toctree: _autosummary
  :nosignatures:

    ABI7500FastDx
    ABI7500DeviceError
    ABI7500Identity
    ABI7500Status
    ThermalStep
    ThermalCycle
    ThermalProgram

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
