"""Setup dialog and options. configuration.yaml (`ems:`) keeps working: it is imported into the same
config entry on every start and then takes precedence over the options set in the dialog."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_BATTERY_POWER,
    CONF_CAPACITY,
    CONF_CHARGED,
    CONF_CHARGED_PV,
    CONF_CONSUMED,
    CONF_DISCHARGE_MAX,
    CONF_EMS_MODE,
    CONF_FEED_IN_CT,
    CONF_FORCED,
    CONF_FORCED_POWER,
    CONF_FRONIUS_HOST,
    CONF_FRONIUS_PORT,
    CONF_FRONIUS_REVERT_S,
    CONF_FRONIUS_UNIT,
    CONF_GRID_CHARGE_POWER,
    CONF_GRID_POWER,
    CONF_IMPORTED,
    CONF_INVERTER,
    CONF_LOAD_POWER,
    CONF_LOAD_PROFILE,
    CONF_MEASUREMENT,
    CONF_NOTIFY,
    CONF_PRICE_NOW,
    CONF_PRICES,
    CONF_PV,
    CONF_PV_POWER,
    CONF_RECORDING,
    CONF_RESERVE,
    CONF_SOC,
    CONF_TARGET,
    CONF_WEAR,
    DOMAIN,
    EXECUTOR_KEYS,
    FRONIUS_KEYS,
    FRONIUS_PORT_DEFAULT,
    FRONIUS_REVERT_MAX_S,
    FRONIUS_UNIT_DEFAULT,
    INVERTER_FRONIUS,
    INVERTER_SUNGROW,
    INVERTERS,
    MEASUREMENT_KEYS,
    OPTION_DEFAULTS,
    PLANNER_KEYS,
    REQUIRED_KEYS,
)

TITLE = "Expensive Mini Savings"

DOMAINS = {
    CONF_PRICES: ["sensor"],
    CONF_PV: ["sensor"],
    CONF_LOAD_PROFILE: ["sensor"],
    CONF_SOC: ["sensor"],
    CONF_CAPACITY: ["sensor", "input_number", "number"],
    CONF_RESERVE: ["number", "input_number", "sensor"],
    CONF_WEAR: ["input_number", "number", "sensor"],
    CONF_GRID_CHARGE_POWER: ["input_number", "number", "sensor"],
    CONF_TARGET: ["sensor"],
    CONF_LOAD_POWER: ["sensor"],
    CONF_PV_POWER: ["sensor"],
    CONF_BATTERY_POWER: ["sensor"],
    CONF_GRID_POWER: ["sensor"],
    CONF_PRICE_NOW: ["sensor"],
    CONF_EMS_MODE: ["select", "input_select", "sensor"],
    CONF_FORCED: ["select", "input_select", "sensor"],
    CONF_DISCHARGE_MAX: ["number", "input_number", "sensor"],
    CONF_FORCED_POWER: ["number", "input_number"],
    CONF_CHARGED: ["sensor"],
    CONF_CHARGED_PV: ["sensor"],
    CONF_IMPORTED: ["sensor"],
    CONF_CONSUMED: ["sensor"],
}


def merged_config(entry: ConfigEntry) -> dict[str, Any]:
    """Effective configuration: defaults ← entry data ← options. For an entry imported from
    configuration.yaml the YAML wins, options are ignored."""
    options = {} if entry.source == SOURCE_IMPORT else dict(entry.options)
    return {**OPTION_DEFAULTS, **entry.data, **options}


def format_settings(conf: dict[str, Any]) -> str:
    """Markdown list of the effective settings (keys as in configuration.yaml)."""
    keys = [
        *PLANNER_KEYS,
        CONF_FEED_IN_CT,
        CONF_RECORDING,
        CONF_MEASUREMENT,
        *MEASUREMENT_KEYS,
        *EXECUTOR_KEYS,
        CONF_NOTIFY,
        CONF_INVERTER,
        *(FRONIUS_KEYS if conf.get(CONF_INVERTER) == INVERTER_FRONIUS else ()),
    ]

    def value(key: str) -> Any:
        if conf.get(key) not in (None, ""):
            return conf.get(key)
        return "Tibber (tibber.get_prices)" if key == CONF_PRICES else "–"

    return "\n".join(f"- `{key}`: {value(key)}" for key in keys)


def _entity(key: str) -> selector.EntitySelector:
    return selector.EntitySelector(selector.EntitySelectorConfig(domain=DOMAINS[key]))


def _planner_schema() -> vol.Schema:
    fields: dict[Any, Any] = {}
    for key in PLANNER_KEYS:
        marker = vol.Required if key in REQUIRED_KEYS else vol.Optional
        fields[marker(key)] = _entity(key)
    fields[vol.Required(CONF_FEED_IN_CT)] = selector.NumberSelector(
        selector.NumberSelectorConfig(min=0, max=50, step=0.1, unit_of_measurement="ct/kWh", mode="box")
    )
    fields[vol.Required(CONF_RECORDING)] = selector.BooleanSelector()
    return vol.Schema(fields)


def _measurement_schema() -> vol.Schema:
    fields: dict[Any, Any] = {vol.Required(CONF_MEASUREMENT): selector.BooleanSelector()}
    for key in MEASUREMENT_KEYS:
        fields[vol.Optional(key)] = _entity(key)
    return vol.Schema(fields)


CONTROL_SUGGESTED = {
    CONF_INVERTER: INVERTER_SUNGROW,
    CONF_FRONIUS_PORT: FRONIUS_PORT_DEFAULT,
    CONF_FRONIUS_UNIT: FRONIUS_UNIT_DEFAULT,
    CONF_FRONIUS_REVERT_S: 0,
}
"""Suggested in the dialog only; not stored unless the user keeps them (see const.OPTION_DEFAULTS)."""


def _number(low: int, high: int, unit: str | None = None) -> selector.NumberSelector:
    config = selector.NumberSelectorConfig(min=low, max=high, step=1, mode="box")
    if unit:
        config["unit_of_measurement"] = unit
    return selector.NumberSelector(config)


def _control_schema() -> vol.Schema:
    fields: dict[Any, Any] = {
        vol.Optional(CONF_NOTIFY): selector.TextSelector(),
        vol.Optional(CONF_INVERTER): selector.SelectSelector(
            selector.SelectSelectorConfig(options=list(INVERTERS), translation_key=CONF_INVERTER)
        ),
        vol.Optional(CONF_FRONIUS_HOST): selector.TextSelector(),
        vol.Optional(CONF_FRONIUS_PORT): _number(1, 65535),
        vol.Optional(CONF_FRONIUS_UNIT): _number(0, 255),
        vol.Optional(CONF_FRONIUS_REVERT_S): _number(0, FRONIUS_REVERT_MAX_S, "s"),
    }
    for key in EXECUTOR_KEYS:
        fields[vol.Optional(key)] = _entity(key)
    return vol.Schema(fields)


def _control_input(user_input: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Clean the control step: whole numbers, Fronius fields only for Fronius; errors by field."""
    data = dict(user_input)
    if data.get(CONF_INVERTER) != INVERTER_FRONIUS:
        for key in FRONIUS_KEYS:
            data.pop(key, None)
        return data, {}
    for key in (CONF_FRONIUS_PORT, CONF_FRONIUS_UNIT, CONF_FRONIUS_REVERT_S):
        if data.get(key) is not None:
            data[key] = int(data[key])
    host = (data.get(CONF_FRONIUS_HOST) or "").strip()
    data[CONF_FRONIUS_HOST] = host
    return data, ({} if host else {CONF_FRONIUS_HOST: "fronius_host"})


class EmsConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_measurement()
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(_planner_schema(), OPTION_DEFAULTS),
        )

    async def async_step_measurement(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_control()
        return self.async_show_form(
            step_id="measurement",
            data_schema=self.add_suggested_values_to_schema(_measurement_schema(), OPTION_DEFAULTS),
        )

    async def async_step_control(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = _control_input(user_input)
            if not errors:
                return self.async_create_entry(title=TITLE, data={**self._data, **data})
        return self.async_show_form(
            step_id="control",
            data_schema=self.add_suggested_values_to_schema(
                _control_schema(), {**CONTROL_SUGGESTED, **OPTION_DEFAULTS, **(user_input or {})}
            ),
            errors=errors,
        )

    async def async_step_import(self, import_data: dict[str, Any]) -> ConfigFlowResult:
        """configuration.yaml → config entry. Runs on every start and replaces the entry's data with the
        YAML, so keys removed from the YAML disappear (up to 0.8.0 they were merged and stayed)."""
        entry = await self.async_set_unique_id(DOMAIN)
        if entry is not None:
            if dict(entry.data) != dict(import_data):
                self.hass.config_entries.async_update_entry(entry, data=dict(import_data))
            return self.async_abort(reason="already_configured")
        return self.async_create_entry(title=TITLE, data=dict(import_data))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> EmsOptionsFlow:
        return EmsOptionsFlow()


class EmsOptionsFlow(OptionsFlow):
    """Change the inputs later (Settings → Devices & services → Expensive Mini Savings → Configure)."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self.config_entry.source == SOURCE_IMPORT:
            # read-only: show the settings from configuration.yaml
            return self.async_abort(
                reason="yaml",
                description_placeholders={"settings": format_settings(merged_config(self.config_entry))},
            )
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_measurement()
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                _planner_schema(), merged_config(self.config_entry)
            ),
        )

    async def async_step_measurement(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_control()
        return self.async_show_form(
            step_id="measurement",
            data_schema=self.add_suggested_values_to_schema(
                _measurement_schema(), merged_config(self.config_entry)
            ),
        )

    async def async_step_control(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = _control_input(user_input)
            if not errors:
                return self.async_create_entry(data={**self._data, **data})
        suggested = {**CONTROL_SUGGESTED, **merged_config(self.config_entry), **(user_input or {})}
        return self.async_show_form(
            step_id="control",
            data_schema=self.add_suggested_values_to_schema(_control_schema(), suggested),
            errors=errors,
        )
