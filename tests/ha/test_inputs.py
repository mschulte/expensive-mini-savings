"""Inputs: reading and parsing the configured HA states in one place."""

from __future__ import annotations

from custom_components.ems import control as c
from custom_components.ems.const import (
    CONF_DISCHARGE_MAX,
    CONF_EMS_MODE,
    CONF_FORCED,
    CONF_IMPORTED,
    CONF_SOC,
    DEFAULTS,
)
from custom_components.ems.driver import SungrowDriver
from custom_components.ems.inputs import Inputs


def test_values_are_parsed_once(hass):
    inp = Inputs(hass, dict(DEFAULTS))
    hass.states.async_set(DEFAULTS[CONF_SOC], "unavailable")
    assert inp.soc() is None
    hass.states.async_set(DEFAULTS[CONF_SOC], "nan")
    assert inp.soc() is None
    hass.states.async_set(DEFAULTS[CONF_SOC], "45.5")
    assert inp.soc() == 45.5
    hass.states.async_set(DEFAULTS[CONF_IMPORTED], "500.0")
    assert inp.counters() == c.Counters(charged=0.0, charged_pv=0.0, imported=500.0)
    assert Inputs(hass, {}).soc() is None  # not configured


def test_sungrow_reading(hass):
    conf = dict(DEFAULTS)
    inp = SungrowDriver(hass, conf, Inputs(hass, conf))
    assert not inp.reading().ready
    hass.states.async_set(DEFAULTS[CONF_EMS_MODE], c.SELF_CONSUMPTION)
    hass.states.async_set(DEFAULTS[CONF_FORCED], c.FORCED_STOP)
    hass.states.async_set(DEFAULTS[CONF_DISCHARGE_MAX], "10.0", {"max": 4000})
    reading = inp.reading()
    assert reading.ready and reading.state == c.HOLD and reading.action_code == "h"
    assert reading.changed_at == hass.states.get(DEFAULTS[CONF_DISCHARGE_MAX]).last_changed
    assert inp.discharge_upper_w() == 4000
    hass.states.async_set(DEFAULTS[CONF_EMS_MODE], "VPP")
    assert inp.reading().state == "Anders (VPP / Stop (default) / 10 W)"
    hass.states.async_set(DEFAULTS[CONF_EMS_MODE], c.SELF_CONSUMPTION)
    hass.states.async_set(DEFAULTS[CONF_DISCHARGE_MAX], "5900", {})
    assert inp.reading().state == c.NORMAL and inp.discharge_upper_w() == 5900
