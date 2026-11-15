"""The executor with the Fronius driver in a test Home Assistant. The inverter is simulated as a register
image (tests/fronius_sim.py) in place of the Modbus TCP connection."""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from test_executor import block_now, with_plan
from test_integration import ENTITIES

from custom_components.ems import control as c
from custom_components.ems import driver as drv
from custom_components.ems import fronius as fr
from tests.fronius_sim import FakeFronius, raw


@pytest.fixture
def sim(monkeypatch):
    fake = FakeFronius(max_charge_w=5000)
    monkeypatch.setattr(drv, "ModbusTcpClient", lambda *_args, **_kw: fake)
    return fake


async def setup_fronius(hass, set_states, tmp_path, **extra):
    hass.config.config_dir = str(tmp_path)
    await hass.config.async_set_time_zone("Europe/Berlin")
    set_states(**{"sensor.battery_level": "45.0"})
    conf = {
        k: v
        for k, v in ENTITIES.items()
        if k not in ("verschleiss", "netzladeleistung", "soll", "lastprofil")
    }
    fronius = {
        "wechselrichter": "fronius",
        "fronius_host": "192.0.2.10",
        "soc": "sensor.battery_level",
        **extra,
    }
    assert await async_setup_component(
        hass, "ems", {"ems": {**conf, **fronius, "aufzeichnung": False, "messung": False}}
    )
    await hass.async_block_till_done()
    (entry,) = hass.config_entries.async_entries("ems")
    return entry.runtime_data.executor


@pytest.fixture
async def ems(hass, set_states, tmp_path, sim):
    return await setup_fronius(hass, set_states, tmp_path)


def control(sim: FakeFronius) -> tuple[int, float]:
    st = sim.storage()
    return st.control_mode, st.discharge_rate_pct


async def test_reads_the_inverter(hass, ems, sim):
    assert isinstance(ems.driver, drv.FroniusDriver)
    assert ems.inverter() == c.NORMAL
    attributes = hass.states.get("sensor.ems_akku_soll").attributes
    assert attributes["ist"] == c.NORMAL
    assert attributes["wechselrichter_werte"]["storctl_mod"] == 0
    assert attributes["wechselrichter_werte"]["max_ladeleistung_w"] == 5000


async def test_only_planning_never_writes(hass, ems, sim, monkeypatch):
    with_plan(ems, monkeypatch, [block_now("h")])
    ems.evaluate()
    await ems.apply("pruefen")
    assert sim.writes == []


async def test_automatic_hold_and_back(hass, ems, sim, monkeypatch):
    with_plan(ems, monkeypatch, [block_now("h", spart=0.6, netz=2.0)])
    await ems.set_mode(c.AUTOMATIC)
    await hass.async_block_till_done()
    assert control(sim) == (fr.DISCHARGE_LIMIT, 0)
    assert sim.writes[-1] == (fr.REG_STOR_CTL_MOD, fr.DISCHARGE_LIMIT)
    assert ems.state["set_by_ems"] == c.HOLD
    assert ems.state["log"][0]["art"] == "Geschaltet"
    assert ems.inverter() == c.HOLD

    with_plan(ems, monkeypatch, [])
    ems.evaluate()
    await hass.async_block_till_done()
    assert control(sim) == (0, 100)
    assert ems.state["set_by_ems"] == c.NORMAL


async def test_grid_charge_with_the_set_power(hass, ems, sim, monkeypatch):
    with_plan(ems, monkeypatch, [block_now("l", kwh=4.0, spart=0.8)])
    await ems.set_number("grid_charge_kw", 3.5)
    await ems.set_mode(c.AUTOMATIC)
    await hass.async_block_till_done()
    assert sim.get(fr.OUT_W_RTE) == raw(-7000)  # 3.5 of 5 kW
    assert sim.get(fr.CHA_GRI_SET) == 1
    assert ems.inverter() == c.CHARGE and ems.state["set_by_ems"] == c.CHARGE


async def test_foreign_setting_is_not_overridden(hass, ems, sim, monkeypatch, freezer):
    sim.set(fr.STOR_CTL_MOD, fr.CHARGE_LIMIT)  # e.g. a schedule in the Fronius app
    await ems.driver.async_refresh()
    assert ems.inverter().startswith("Anders (StorCtl 1")
    freezer.tick(timedelta(minutes=6))
    with_plan(ems, monkeypatch, [block_now("h")])
    await ems.set_mode(c.AUTOMATIC)
    await hass.async_block_till_done()
    assert sim.writes == []
    assert ems.state["log"][0]["art"] == "Manuell"


async def test_unconfirmed_write_is_logged_as_error(hass, ems, sim, monkeypatch):
    monkeypatch.setattr("custom_components.ems.executor.CONFIRM_S", 0.05)
    with_plan(ems, monkeypatch, [block_now("h")])
    sim.stuck = True
    await ems.set_mode(c.AUTOMATIC)
    await hass.async_block_till_done()
    assert ems.state["log"][0]["art"] == "Fehler"
    assert ems.state["set_by_ems"] == c.NORMAL


async def test_unreachable_after_repeated_failures(hass, ems, sim):
    sim.down = True
    for _ in range(drv.UNREACHABLE_AFTER - 1):
        await ems.driver.async_refresh()
    assert ems.inverter() == c.NORMAL  # a single timeout is no outage
    await ems.driver.async_refresh()
    assert ems.inverter() == c.UNREACHABLE
    sim.down = False
    await ems.driver.async_refresh()
    assert ems.inverter() == c.NORMAL


async def test_wrong_register_layout_never_writes(hass, set_states, tmp_path, monkeypatch):
    fake = FakeFronius(header=(124, 26))  # float layout
    monkeypatch.setattr(drv, "ModbusTcpClient", lambda *_args, **_kw: fake)
    ex = await setup_fronius(hass, set_states, tmp_path)
    for _ in range(drv.UNREACHABLE_AFTER):
        await ex.driver.async_refresh()
    assert ex.inverter() == c.UNREACHABLE
    assert "int + SF" in ex.driver.reading().detail
    with_plan(ex, monkeypatch, [block_now("h")])
    await ex.set_mode(c.AUTOMATIC)
    await hass.async_block_till_done()
    assert fake.writes == []


async def test_revert_timer_is_refreshed_while_holding(hass, set_states, tmp_path, sim, monkeypatch):
    ex = await setup_fronius(hass, set_states, tmp_path, fronius_ruecksetzen_s=60)
    with_plan(ex, monkeypatch, [block_now("h")])
    await ex.set_mode(c.AUTOMATIC)
    await hass.async_block_till_done()
    assert sim.get(fr.RVRT_TMS) == 60
    written = len(sim.writes)
    clock = [drv.time.monotonic() + 31]
    monkeypatch.setattr(drv.time, "monotonic", lambda: clock[0])
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=drv.POLL_S + 1))
    await hass.async_block_till_done()
    assert len(sim.writes) > written
    assert sim.writes[-1] == (fr.REG_STOR_CTL_MOD, fr.DISCHARGE_LIMIT)


async def test_fronius_needs_a_host(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    assert not await async_setup_component(hass, "ems", {"ems": {"wechselrichter": "fronius"}})


def _schema_keys(flow) -> set[str]:
    return {str(k) for k in flow["data_schema"].schema}


async def test_dialog_sets_up_the_fronius(hass, set_states, tmp_path, sim):
    hass.config.config_dir = str(tmp_path)
    await hass.config.async_set_time_zone("Europe/Berlin")
    set_states(**{"sensor.battery_level": "45.0"})
    flow = await hass.config_entries.flow.async_init("ems", context={"source": config_entries.SOURCE_USER})
    assert flow["step_id"] == "user"
    flow = await hass.config_entries.flow.async_configure(flow["flow_id"], {"wechselrichter": "fronius"})
    assert flow["step_id"] == "fronius"
    flow = await hass.config_entries.flow.async_configure(
        flow["flow_id"],
        {"fronius_host": " ", "fronius_port": 502, "fronius_unit_id": 1, "fronius_ruecksetzen_s": 0},
    )
    assert flow["step_id"] == "fronius" and flow["errors"] == {"fronius_host": "fronius_host"}
    flow = await hass.config_entries.flow.async_configure(
        flow["flow_id"],
        {
            "fronius_host": " 192.0.2.10 ",
            "fronius_port": 502.0,
            "fronius_unit_id": 1.0,
            "fronius_ruecksetzen_s": 0,
        },
    )
    assert flow["step_id"] == "planner"
    flow = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"soc": "sensor.battery_level", "einspeisung_ct": 7.0, "aufzeichnung": False}
    )
    assert flow["step_id"] == "measurement"
    assert not _schema_keys(flow) & {"ems_modus", "erzwungen", "entladen_max"}  # Sungrow only
    flow = await hass.config_entries.flow.async_configure(flow["flow_id"], {"messung": False})
    assert flow["step_id"] == "control"
    assert "ladeleistung_erzwungen" not in _schema_keys(flow)
    flow = await hass.config_entries.flow.async_configure(flow["flow_id"], {})
    assert flow["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    (entry,) = hass.config_entries.async_entries("ems")
    assert entry.data["fronius_host"] == "192.0.2.10" and entry.data["fronius_port"] == 502
    conf = entry.runtime_data.conf
    assert "ems_modus" not in conf and "akku_ladung" not in conf  # no Sungrow defaults
    assert isinstance(entry.runtime_data.executor.driver, drv.FroniusDriver)
    assert entry.runtime_data.executor.inverter() == c.NORMAL


async def test_sungrow_defaults_only_for_a_sungrow(hass, set_states, tmp_path, sim):
    ex = await setup_fronius(hass, set_states, tmp_path)
    conf = ex.conf
    assert conf["soc"] == "sensor.battery_level"  # configured, not a default
    for key in ("ems_modus", "erzwungen", "entladen_max", "ladeleistung_erzwungen", "kapazitaet"):
        assert key not in conf


async def test_sungrow_keys_are_ignored_for_a_fronius(hass, set_states, tmp_path, sim):
    ex = await setup_fronius(hass, set_states, tmp_path, ems_modus="select.something")
    assert "ems_modus" not in ex.conf


async def test_fronius_needs_the_state_of_charge(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    conf = {"wechselrichter": "fronius", "fronius_host": "192.0.2.10"}
    assert not await async_setup_component(hass, "ems", {"ems": conf})
