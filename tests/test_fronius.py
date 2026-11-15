"""Fronius storage model: decoding, the same classification as the Sungrow, and the register writes."""

from __future__ import annotations

import asyncio

import pytest

from custom_components.ems import fronius as fr
from custom_components.ems.inverter import InverterSetting
from tests.fronius_sim import FakeFronius, raw


def apply(sim: FakeFronius, writes: list[tuple[int, int]]) -> None:
    for address, value in writes:
        asyncio.run(sim.write_register(address, value))


def test_decode_scales_with_the_scale_factors():
    st = FakeFronius(max_charge_w=10240, soc_pct=63.5).storage()
    assert st.max_charge_w == 10240
    assert st.soc_pct == pytest.approx(63.5)
    assert st.min_reserve_pct == pytest.approx(5.0)
    assert st.discharge_rate_pct == pytest.approx(100.0)
    assert st.rate_sf == -2
    assert st.grid_charging


def test_decode_signed_rates_and_missing_scale_factor():
    sim = FakeFronius()
    sim.set(fr.OUT_W_RTE, -2500)
    sim.set(fr.IN_OUT_W_RTE_SF, -32768)  # not implemented: the default -2
    st = sim.storage()
    assert st.discharge_rate_pct == pytest.approx(-25.0)
    assert st.rate_sf == fr.RATE_SF_DEFAULT


def test_decode_needs_the_whole_block():
    with pytest.raises(ValueError):
        fr.Storage.decode([0] * 10)


def test_model_check():
    assert fr.model_ok([124, 24])
    assert not fr.model_ok([124, 26])  # another layout (float)
    assert not fr.model_ok([0, 0])
    assert not fr.model_ok([])


@pytest.mark.parametrize(
    ("mode", "out_pct", "grid", "setting"),
    [
        (0, 100, True, InverterSetting.NORMAL),
        (0, 0, False, InverterSetting.NORMAL),  # rates count only with a limit set
        (2, 0, True, InverterSetting.HOLD),
        (2, 0, False, InverterSetting.HOLD),
        (2, -50, True, InverterSetting.CHARGE),
        (2, -50, False, InverterSetting.OTHER),  # forced charge without the grid
        (2, 30, True, InverterSetting.OTHER),  # limited discharge
        (1, 100, True, InverterSetting.OTHER),  # charge limit (e.g. batcontrol mode 8)
        (3, 0, True, InverterSetting.OTHER),
    ],
)
def test_classify(mode, out_pct, grid, setting):
    sim = FakeFronius()
    sim.set(fr.STOR_CTL_MOD, mode)
    sim.set(fr.OUT_W_RTE, out_pct * 100)
    sim.set(fr.CHA_GRI_SET, 1 if grid else 0)
    assert fr.classify(sim.storage()) is setting


@pytest.mark.parametrize("start_mode", [0, 1, 2, 3])
@pytest.mark.parametrize("goal", [InverterSetting.NORMAL, InverterSetting.HOLD, InverterSetting.CHARGE])
def test_writes_reach_the_goal_from_any_state(goal, start_mode):
    sim = FakeFronius()
    sim.set(fr.STOR_CTL_MOD, start_mode)
    sim.set(fr.OUT_W_RTE, 3000)
    sim.set(fr.CHA_GRI_SET, 0)
    apply(sim, fr.writes_for(goal, grid_w=2500, storage=sim.storage()))
    assert fr.classify(sim.storage()) is goal


def test_grid_charge_power_is_a_share_of_wchamax():
    sim = FakeFronius(max_charge_w=5000)
    writes = dict(fr.writes_for(InverterSetting.CHARGE, grid_w=3500, storage=sim.storage()))
    assert writes[fr.REG_OUT_W_RTE] == raw(-7000)  # −70 % with scale factor −2
    assert writes[fr.REG_CHA_GRI_SET] == 1
    capped = dict(fr.writes_for(InverterSetting.CHARGE, grid_w=9000, storage=sim.storage()))
    assert capped[fr.REG_OUT_W_RTE] == raw(-10000)


def test_control_mode_is_written_last_when_limiting_and_first_when_releasing():
    st = FakeFronius().storage()
    for goal in (InverterSetting.HOLD, InverterSetting.CHARGE):
        assert fr.writes_for(goal, grid_w=1000, storage=st)[-1] == (fr.REG_STOR_CTL_MOD, fr.DISCHARGE_LIMIT)
    assert fr.writes_for(InverterSetting.NORMAL, grid_w=0, storage=st)[0] == (fr.REG_STOR_CTL_MOD, 0)


def test_revert_timer_is_set_with_a_limit_and_cleared_on_release():
    st = FakeFronius().storage()
    assert (fr.REG_RVRT_TMS, 900) in fr.writes_for(InverterSetting.HOLD, grid_w=0, storage=st, revert_s=900)
    assert (fr.REG_RVRT_TMS, 0) in fr.writes_for(InverterSetting.NORMAL, grid_w=0, storage=st, revert_s=900)


def test_grid_charge_needs_wchamax():
    sim = FakeFronius(max_charge_w=0)
    with pytest.raises(ValueError):
        fr.writes_for(InverterSetting.CHARGE, grid_w=1000, storage=sim.storage())


def test_no_writes_for_other_settings():
    with pytest.raises(ValueError):
        fr.writes_for(InverterSetting.OTHER, grid_w=0, storage=FakeFronius().storage())


def test_describe():
    sim = FakeFronius()
    sim.set(fr.STOR_CTL_MOD, 1)
    sim.set(fr.IN_W_RTE, 2000)
    assert sim.storage().describe() == "StorCtl 1, Entladen 100 %, Laden 20 %"
