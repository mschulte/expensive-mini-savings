"""Fronius GEN24 battery control through SunSpec Modbus TCP (storage model 124). No HA import: decoding,
classification and the register writes are table-tested like switching.py.

Register addresses are sent as written (PDU address), as batcontrol's fronius-modbus backend does
(github.com/MaStr/batcontrol, MIT). The inverter must be set to "int + SF" in its Modbus settings; the
model check (ID 124, length 24 at MODEL_HEADER) guards against another layout.

How the three actions map:

- normal:      StorCtl_Mod = 0 (no limit; the inverter's own battery management decides)
- hold:        StorCtl_Mod = 2 (discharge limit), OutWRte = 0 %: the battery does not discharge, PV surplus
               keeps charging it (no charge limit)
- grid charge: ChaGriSet = 1, StorCtl_Mod = 2, OutWRte = -x % of WChaMax: a negative discharge rate
               charges, from the grid where PV is not enough
"""

from __future__ import annotations

from dataclasses import dataclass

from .inverter import InverterSetting

MODEL_HEADER = 40343
"""ID and length of the storage model; the points start right after."""
MODEL_ID = 124
MODEL_LENGTH = 24
STORAGE_START = 40345
STORAGE_COUNT = 24

# offsets in the storage block (SunSpec model 124, int + SF)
W_CHA_MAX = 0
STOR_CTL_MOD = 3
MIN_RSV_PCT = 5
CHA_STATE = 6
CHA_ST = 9
OUT_W_RTE = 10
IN_W_RTE = 11
RVRT_TMS = 13
CHA_GRI_SET = 15
W_CHA_MAX_SF = 16
MIN_RSV_PCT_SF = 19
CHA_STATE_SF = 20
IN_OUT_W_RTE_SF = 23

REG_STOR_CTL_MOD = STORAGE_START + STOR_CTL_MOD
REG_OUT_W_RTE = STORAGE_START + OUT_W_RTE
REG_IN_W_RTE = STORAGE_START + IN_W_RTE
REG_RVRT_TMS = STORAGE_START + RVRT_TMS
REG_CHA_GRI_SET = STORAGE_START + CHA_GRI_SET

CHARGE_LIMIT = 1
DISCHARGE_LIMIT = 2
"""StorCtl_Mod bits."""
GRID_CHARGING = 1
"""ChaGriSet: 1 = charging from the grid allowed."""
RATE_SF_DEFAULT = -2
"""Scale factor of the rates if the inverter reports none (100 % = 10000)."""
SF_NOT_IMPLEMENTED = -32768
NOT_IMPLEMENTED = 0xFFFF


def signed16(raw: int) -> int:
    return raw - 0x10000 if raw & 0x8000 else raw


def unsigned16(value: int) -> int:
    return value & 0xFFFF


@dataclass(frozen=True)
class Storage:
    """The storage model as read now (decoded)."""

    max_charge_w: float | None
    """WChaMax: reference for the charge and discharge rates."""
    control_mode: int
    """StorCtl_Mod: bit 0 charge limit, bit 1 discharge limit."""
    min_reserve_pct: float | None
    soc_pct: float | None
    charge_status: int
    discharge_rate_pct: float
    """OutWRte in % of WChaMax; negative = charging."""
    charge_rate_pct: float
    revert_s: int
    grid_charging: bool
    rate_sf: int

    @classmethod
    def decode(cls, regs: list[int]) -> Storage:
        if len(regs) < STORAGE_COUNT:
            raise ValueError(f"storage model: {len(regs)} registers, expected {STORAGE_COUNT}")

        def sf(offset: int, default: int | None = None) -> int | None:
            v = signed16(regs[offset])
            return default if v == SF_NOT_IMPLEMENTED else v

        def scaled(offset: int, scale: int | None, signed: bool = False) -> float | None:
            raw = regs[offset]
            if (raw == NOT_IMPLEMENTED and not signed) or scale is None:
                return None
            return (signed16(raw) if signed else raw) * 10.0**scale

        rate_sf = sf(IN_OUT_W_RTE_SF, RATE_SF_DEFAULT)
        assert rate_sf is not None
        return cls(
            max_charge_w=scaled(W_CHA_MAX, sf(W_CHA_MAX_SF, 0)),
            control_mode=regs[STOR_CTL_MOD],
            min_reserve_pct=scaled(MIN_RSV_PCT, sf(MIN_RSV_PCT_SF)),
            soc_pct=scaled(CHA_STATE, sf(CHA_STATE_SF)),
            charge_status=regs[CHA_ST],
            discharge_rate_pct=scaled(OUT_W_RTE, rate_sf, signed=True) or 0.0,
            charge_rate_pct=scaled(IN_W_RTE, rate_sf, signed=True) or 0.0,
            revert_s=regs[RVRT_TMS],
            grid_charging=regs[CHA_GRI_SET] == GRID_CHARGING,
            rate_sf=rate_sf,
        )

    def describe(self) -> str:
        """For 'Anders (…)' and the log: the raw control state in short."""
        return (
            f"StorCtl {self.control_mode}, Entladen {self.discharge_rate_pct:g} %, "
            f"Laden {self.charge_rate_pct:g} %"
        )


def model_ok(header: list[int]) -> bool:
    """The storage model is where this driver expects it (int + SF layout)."""
    return len(header) >= 2 and header[0] == MODEL_ID and header[1] == MODEL_LENGTH


def classify(st: Storage) -> InverterSetting:
    """The setting at the inverter, in the same terms as the Sungrow (inverter.classify)."""
    mode = st.control_mode
    if mode == 0:
        return InverterSetting.NORMAL
    if mode == DISCHARGE_LIMIT:
        rate = st.discharge_rate_pct
        if rate == 0:
            return InverterSetting.HOLD
        if rate < 0 and st.grid_charging:
            return InverterSetting.CHARGE
    return InverterSetting.OTHER


def rate_value(pct: float, rate_sf: int) -> int:
    """A rate in % as register value (signed, two's complement)."""
    return unsigned16(round(pct / 10.0**rate_sf))


def writes_for(
    setting: InverterSetting, *, grid_w: float, storage: Storage, revert_s: int = 0
) -> list[tuple[int, int]]:
    """The register writes (address, value) for a target setting, in the order they must be sent: the
    rates first, the control mode last, so no intermediate state discharges or charges unintended."""
    sf = storage.rate_sf
    full = rate_value(100, sf)
    revert = max(0, min(int(revert_s), 0xFFFF))
    if setting is InverterSetting.CHARGE:
        max_w = storage.max_charge_w or 0.0
        if max_w <= 0:
            raise ValueError("WChaMax unknown: grid charge power cannot be set")
        pct = max(0.0, min(100.0, grid_w / max_w * 100))
        return [
            (REG_CHA_GRI_SET, GRID_CHARGING),
            (REG_RVRT_TMS, revert),
            (REG_OUT_W_RTE, rate_value(-pct, sf)),
            (REG_IN_W_RTE, full),
            (REG_STOR_CTL_MOD, DISCHARGE_LIMIT),
        ]
    if setting is InverterSetting.HOLD:
        return [
            (REG_RVRT_TMS, revert),
            (REG_OUT_W_RTE, 0),
            (REG_IN_W_RTE, full),
            (REG_STOR_CTL_MOD, DISCHARGE_LIMIT),
        ]
    if setting is InverterSetting.NORMAL:
        # the control mode first: from then on the inverter ignores the rates
        return [
            (REG_STOR_CTL_MOD, 0),
            (REG_OUT_W_RTE, full),
            (REG_IN_W_RTE, full),
            (REG_RVRT_TMS, 0),
        ]
    raise ValueError(f"no writes for {setting}")
