"""The integration's view of Home Assistant states: which entity a config key points to, and its value
parsed. Coordinator, executor and recorder share one instance; none of them reads hass.states itself."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant, State

from . import control as c
from .const import CONF_CHARGED, CONF_CHARGED_PV, CONF_CONSUMED, CONF_IMPORTED, CONF_SOC
from .measurement import as_number

ENERGY_UNITS = {"Wh": 0.001, "kWh": 1.0, "MWh": 1000.0}


class Inputs:
    def __init__(self, hass: HomeAssistant, conf: dict[str, Any]) -> None:
        self.hass = hass
        self.conf = conf

    # ---- generic ----

    def configured(self, key: str) -> bool:
        return bool(self.conf.get(key))

    def state(self, key: str) -> State | None:
        entity_id = self.conf.get(key)
        return self.hass.states.get(entity_id) if entity_id else None

    def value(self, key: str) -> str | None:
        st = self.state(key)
        return st.state if st else None

    def number(self, key: str) -> float | None:
        """The state as a number; None if missing, unavailable or not a number."""
        return as_number(self.value(key))

    def attribute(self, key: str, name: str) -> Any:
        st = self.state(key)
        return st.attributes.get(name) if st else None

    # ---- typed ----

    def soc(self) -> float | None:
        """Battery state of charge in % of the usable range."""
        return self.number(CONF_SOC)

    def kwh(self, key: str) -> float | None:
        """An energy value in kWh, converted by the entity's unit (Wh, kWh, MWh; no unit = kWh). The
        Fronius integration and the Smart Meter report Wh, the Sungrow integration kWh."""
        value = self.number(key)
        if value is None:
            return None
        unit = str(self.attribute(key, "unit_of_measurement") or "kWh")
        return value * ENERGY_UNITS.get(unit, 1.0)

    def consumed_kwh(self) -> float | None:
        """Total house consumption meter (for the load profile)."""
        return self.kwh(CONF_CONSUMED)

    def counters(self) -> c.Counters:
        """Meter readings for booking; missing meters count as 0."""
        return c.Counters(
            charged=self.kwh(CONF_CHARGED) or 0.0,
            charged_pv=self.kwh(CONF_CHARGED_PV) or 0.0,
            imported=self.kwh(CONF_IMPORTED) or 0.0,
        )
