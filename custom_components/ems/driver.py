"""Inverter drivers: how the EMS reads what is set at the inverter and how it switches it.

- SungrowDriver: through the entities of the mkaiser Sungrow Modbus integration (select / number).
- FroniusDriver: directly over Modbus TCP (SunSpec storage model 124, see fronius.py); no other
  integration needed.

The executor and the recorder only see InverterReading and the three goals (control.NORMAL, HOLD, CHARGE);
the rules (switching.decide) are the same for every inverter.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.util import dt as dt_util

from . import control as c
from . import fronius as fr
from . import switching as sw
from .const import (
    CONF_DISCHARGE_MAX,
    CONF_EMS_MODE,
    CONF_FORCED,
    CONF_FORCED_POWER,
    CONF_FRONIUS_HOST,
    CONF_FRONIUS_PORT,
    CONF_FRONIUS_REVERT_S,
    CONF_FRONIUS_UNIT,
    CONF_INVERTER,
    FRONIUS_PORT_DEFAULT,
    FRONIUS_UNIT_DEFAULT,
    INVERTER_FRONIUS,
)
from .inputs import Inputs
from .inverter import NO_VALUE, InverterSetting, classify
from .measurement import as_number
from .modbus_tcp import ModbusError, ModbusTcpClient

_LOGGER = logging.getLogger(__name__)

GOAL_SETTING = {
    c.NORMAL: InverterSetting.NORMAL,
    c.HOLD: InverterSetting.HOLD,
    c.CHARGE: InverterSetting.CHARGE,
}


@dataclass(frozen=True)
class InverterReading:
    """What is set at the inverter, as read now."""

    setting: InverterSetting
    ready: bool
    """The inverter reports a complete value (all entities / the Modbus read)."""
    changed_at: datetime | None
    """Last change of the setting (or of any of its entities)."""
    detail: str = ""
    """The raw values in short, shown as 'Anders (…)' for a setting the EMS does not know."""

    @property
    def state(self) -> str:
        """In the executor's terms."""
        return c.state_text(self.setting, self.detail)

    @property
    def action_code(self) -> str:
        """As measured: the setting's code (n, h, l, f, e, v, x, a)."""
        return self.setting.value


class InverterDriver:
    """Interface of a driver. Listeners are called (without arguments) when the reading may have changed."""

    name = ""

    def reading(self) -> InverterReading:
        raise NotImplementedError

    @callback
    def async_listen(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        raise NotImplementedError

    async def async_start(self) -> None:
        """Begin reading (called once at setup)."""

    async def async_stop(self) -> None:
        """Stop reading. Leaves the inverter as it is."""

    async def async_switch(self, goal: str, *, grid_kw: float) -> None:
        """Send the commands for a goal (c.NORMAL, c.HOLD, c.CHARGE). Confirmation comes by reading back."""
        raise NotImplementedError

    def info(self) -> dict[str, Any]:
        """Raw values for the app and diagnostics."""
        return {}


# ---- Sungrow ----------------------------------------------------------------------------------


class SungrowDriver(InverterDriver):
    """Sungrow through the mkaiser Modbus integration: three entities (EMS mode, forced mode, discharge
    limit) are read and set with select.select_option / number.set_value."""

    name = "sungrow"
    KEYS = (CONF_EMS_MODE, CONF_FORCED, CONF_DISCHARGE_MAX)

    def __init__(self, hass: HomeAssistant, conf: dict[str, Any], inputs: Inputs) -> None:
        self.hass = hass
        self.conf = conf
        self.inputs = inputs

    def entities(self) -> list[str]:
        return [e for k in self.KEYS if (e := self.conf.get(k))]

    def reading(self) -> InverterReading:
        states = [self.inputs.state(k) for k in self.KEYS]
        values = [st.state if st else None for st in states]
        changed = [st.last_changed for st in states if st]
        mode, forced, discharge = values[0], values[1], as_number(values[2])
        setting = classify(mode, forced, discharge)
        state = c.inverter_state(mode, forced, discharge)
        return InverterReading(
            setting=setting,
            ready=all(v not in NO_VALUE for v in values),
            changed_at=max(changed) if changed else None,
            detail=state[len("Anders (") : -1] if state.startswith("Anders (") else "",
        )

    @callback
    def async_listen(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        @callback
        def changed(_event: Event) -> None:
            listener()

        return async_track_state_change_event(self.hass, self.entities(), changed)

    def discharge_upper_w(self) -> int:
        """Normal discharge limit = the upper bound of the discharge limit entity."""
        upper = as_number(self.inputs.attribute(CONF_DISCHARGE_MAX, "max"))
        return int(upper) if upper else sw.NORMAL_DISCHARGE_W

    async def async_switch(self, goal: str, *, grid_kw: float) -> None:
        steps = sw.commands_for(
            goal,
            grid_kw=grid_kw,
            power_configured=bool(self.conf.get(CONF_FORCED_POWER)),
            normal_discharge_w=self.discharge_upper_w(),
        )
        for step in steps:
            entity_id = self.conf[step.key]
            if step.kind == "select":
                data = {"entity_id": entity_id, "option": step.value}
                await self.hass.services.async_call("select", "select_option", data, blocking=True)
            else:
                data = {"entity_id": entity_id, "value": step.value}
                await self.hass.services.async_call("number", "set_value", data, blocking=True)


# ---- Fronius ----------------------------------------------------------------------------------

POLL_S = 10
"""How often the storage model is read."""
UNREACHABLE_AFTER = 3
"""Failed reads in a row before the inverter counts as unreachable (a single timeout is no outage)."""


class FroniusDriver(InverterDriver):
    """Fronius GEN24 directly over Modbus TCP. Reads the storage model every POLL_S seconds.

    revert_s > 0 sets the inverter's revert timer (InOutWRte_RvrtTms): if the EMS stops writing (HA down),
    the inverter drops the limits by itself. While a hold or grid charge runs, the driver rewrites it
    after half the time, as long as the inverter still shows what the EMS set."""

    name = "fronius"

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        port: int = FRONIUS_PORT_DEFAULT,
        unit_id: int = FRONIUS_UNIT_DEFAULT,
        revert_s: int = 0,
        client: ModbusTcpClient | None = None,
    ) -> None:
        self.hass = hass
        self.client = client or ModbusTcpClient(host, port, unit_id)
        self.revert_s = revert_s
        self.storage: fr.Storage | None = None
        self.error: str | None = "noch nicht gelesen"
        self._failures = 0
        self._model_checked = False
        self._setting = InverterSetting.UNREACHABLE
        self._changed_at: datetime | None = None
        self._listeners: list[Callable[[], None]] = []
        self._unsub: CALLBACK_TYPE | None = None
        self._written: tuple[InverterSetting, float, float] | None = None
        """What the EMS wrote last: setting, grid charge W, time.monotonic()."""

    # ---- reading ----

    def _ready(self) -> bool:
        return self.storage is not None and self._model_checked and self._failures < UNREACHABLE_AFTER

    def reading(self) -> InverterReading:
        if not self._ready():
            return InverterReading(InverterSetting.UNREACHABLE, False, self._changed_at, self.error or "")
        assert self.storage is not None
        return InverterReading(self._setting, True, self._changed_at, self.storage.describe())

    @callback
    def async_listen(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        self._listeners.append(listener)

        @callback
        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    async def async_start(self) -> None:
        await self.async_refresh()
        self._unsub = async_track_time_interval(self.hass, self._poll, timedelta(seconds=POLL_S))

    async def async_stop(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None
        await self.client.close()

    async def _poll(self, _now: datetime) -> None:
        await self.async_refresh()
        await self._keep_alive()

    async def async_refresh(self) -> None:
        """Read the storage model and tell the listeners if anything changed."""
        before = (self.reading(), self.storage, self.error)
        try:
            if not self._model_checked:
                header = await self.client.read_holding(fr.MODEL_HEADER, 2)
                if not fr.model_ok(header):
                    raise ModbusError(
                        f"SunSpec-Speichermodell 124 nicht an {fr.MODEL_HEADER} (gelesen {header}); "
                        "im Fronius „int + SF“ einstellen"
                    )
                self._model_checked = True
            regs = await self.client.read_holding(fr.STORAGE_START, fr.STORAGE_COUNT)
            storage = fr.Storage.decode(regs)
        except (ModbusError, ValueError) as err:
            self._failures += 1
            self.error = str(err)
            if self._failures == UNREACHABLE_AFTER:
                _LOGGER.warning("Fronius not readable: %s", err)
        else:
            self._failures = 0
            self.error = None
            self.storage = storage
        setting = fr.classify(self.storage) if self._ready() and self.storage else InverterSetting.UNREACHABLE
        if setting is not self._setting:
            self._setting = setting
            self._changed_at = dt_util.now()
        if (self.reading(), self.storage, self.error) != before:
            for listener in list(self._listeners):
                listener()

    # ---- switching ----

    async def async_switch(self, goal: str, *, grid_kw: float) -> None:
        setting = GOAL_SETTING[goal]
        try:
            if self.storage is None:
                await self.async_refresh()
            if self.storage is None:
                raise ModbusError(self.error or "keine Werte")
            await self._write(setting, grid_kw * 1000)
        except (ModbusError, ValueError) as err:
            _LOGGER.warning("Fronius: switching to %s failed: %s", goal, err)
        await self.async_refresh()

    async def _write(self, setting: InverterSetting, grid_w: float) -> None:
        assert self.storage is not None
        writes = fr.writes_for(setting, grid_w=grid_w, storage=self.storage, revert_s=self.revert_s)
        for address, value in writes:
            await self.client.write_register(address, value)
        self._written = (setting, grid_w, time.monotonic())

    async def _keep_alive(self) -> None:
        """Rewrite a running hold / grid charge before the revert timer drops it."""
        if not self.revert_s or self._written is None:
            return
        setting, grid_w, at = self._written
        if setting is InverterSetting.NORMAL or self._setting is not setting:
            return
        if time.monotonic() - at < self.revert_s / 2:
            return
        try:
            await self._write(setting, grid_w)
        except (ModbusError, ValueError) as err:
            _LOGGER.warning("Fronius: refreshing %s failed: %s", setting.name, err)

    def info(self) -> dict[str, Any]:
        st = self.storage
        if st is None:
            return {"fehler": self.error}
        return {
            "storctl_mod": st.control_mode,
            "entladen_prozent": st.discharge_rate_pct,
            "laden_prozent": st.charge_rate_pct,
            "laden_aus_netz": st.grid_charging,
            "max_ladeleistung_w": st.max_charge_w,
            "soc_prozent": st.soc_pct,
            "reserve_prozent": st.min_reserve_pct,
            "ruecksetzen_s": st.revert_s,
            "fehler": self.error,
        }


def make_driver(hass: HomeAssistant, conf: dict[str, Any], inputs: Inputs) -> InverterDriver:
    if conf.get(CONF_INVERTER) == INVERTER_FRONIUS:
        return FroniusDriver(
            hass,
            conf[CONF_FRONIUS_HOST],
            int(conf.get(CONF_FRONIUS_PORT) or FRONIUS_PORT_DEFAULT),
            int(conf.get(CONF_FRONIUS_UNIT) or FRONIUS_UNIT_DEFAULT),
            int(conf.get(CONF_FRONIUS_REVERT_S) or 0),
        )
    return SungrowDriver(hass, conf, inputs)
