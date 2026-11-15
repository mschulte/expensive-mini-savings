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
    SUNGROW_ONLY_KEYS,
    inverter_of,
    with_inverter_defaults,
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


def _raw_config(entry: ConfigEntry) -> dict[str, Any]:
    """defaults ← entry data ← options, without the inverter's entity defaults."""
    options = {} if entry.source == SOURCE_IMPORT else dict(entry.options)
    return {**OPTION_DEFAULTS, **entry.data, **options}


def merged_config(entry: ConfigEntry) -> dict[str, Any]:
    """Effective configuration: defaults ← entry data ← options, plus the entity defaults of the chosen
    inverter (const.with_inverter_defaults). For an entry imported from configuration.yaml the YAML wins,
    options are ignored."""
    return with_inverter_defaults(_raw_config(entry))


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


def _for_inverter(keys: tuple[str, ...], inverter: str) -> list[str]:
    """The keys that apply to an inverter: a Fronius has no Sungrow switching entities."""
    return [k for k in keys if inverter == INVERTER_SUNGROW or k not in SUNGROW_ONLY_KEYS]


def _measurement_schema(inverter: str) -> vol.Schema:
    fields: dict[Any, Any] = {vol.Required(CONF_MEASUREMENT): selector.BooleanSelector()}
    for key in _for_inverter(MEASUREMENT_KEYS, inverter):
        fields[vol.Optional(key)] = _entity(key)
    return vol.Schema(fields)


def _control_schema(inverter: str) -> vol.Schema:
    fields: dict[Any, Any] = {vol.Optional(CONF_NOTIFY): selector.TextSelector()}
    for key in _for_inverter(EXECUTOR_KEYS, inverter):
        fields[vol.Optional(key)] = _entity(key)
    return vol.Schema(fields)


def _number(low: int, high: int, unit: str | None = None) -> selector.NumberSelector:
    config = selector.NumberSelectorConfig(min=low, max=high, step=1, mode="box")
    if unit:
        config["unit_of_measurement"] = unit
    return selector.NumberSelector(config)


INVERTER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_INVERTER, default=INVERTER_SUNGROW): selector.SelectSelector(
            selector.SelectSelectorConfig(options=list(INVERTERS), translation_key=CONF_INVERTER)
        )
    }
)
FRONIUS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_FRONIUS_HOST): selector.TextSelector(),
        vol.Required(CONF_FRONIUS_PORT, default=FRONIUS_PORT_DEFAULT): _number(1, 65535),
        vol.Required(CONF_FRONIUS_UNIT, default=FRONIUS_UNIT_DEFAULT): _number(0, 255),
        vol.Required(CONF_FRONIUS_REVERT_S, default=0): _number(0, FRONIUS_REVERT_MAX_S, "s"),
    }
)


def _fronius_input(user_input: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Clean the Fronius step: whole numbers, trimmed host; errors by field."""
    data = {k: str(v).strip() if k == CONF_FRONIUS_HOST else int(v) for k, v in user_input.items()}
    return data, ({} if data.get(CONF_FRONIUS_HOST) else {CONF_FRONIUS_HOST: "fronius_host"})


class _Steps:
    """The steps shared by the setup dialog and the options: inverter → (Fronius) → planner → measurement
    → control. `_data` collects the answers, `_base` is what the forms start from (the current settings)."""

    _data: dict[str, Any]

    def _base(self) -> dict[str, Any]:
        return dict(OPTION_DEFAULTS)

    def _suggested(self, user_input: dict[str, Any] | None = None) -> dict[str, Any]:
        """Current settings with the entity defaults of the inverter chosen in this dialog."""
        return with_inverter_defaults({**self._base(), **self._data, **(user_input or {})})

    def _inverter(self) -> str:
        return inverter_of(self._data)

    def _finish(self, data: dict[str, Any]) -> ConfigFlowResult:
        raise NotImplementedError

    async def _step_inverter(self, step_id: str, user_input: dict[str, Any] | None) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            if self._inverter() == INVERTER_FRONIUS:
                return await self.async_step_fronius()
            return await self.async_step_planner()
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(  # type: ignore[attr-defined]
                INVERTER_SCHEMA, {CONF_INVERTER: inverter_of(self._base())}
            ),
        )

    async def async_step_fronius(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = _fronius_input(user_input)
            if not errors:
                self._data.update(data)
                return await self.async_step_planner()
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="fronius",
            data_schema=self.add_suggested_values_to_schema(  # type: ignore[attr-defined]
                FRONIUS_SCHEMA, self._suggested(user_input)
            ),
            errors=errors,
        )

    async def async_step_planner(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_measurement()
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="planner",
            data_schema=self.add_suggested_values_to_schema(  # type: ignore[attr-defined]
                _planner_schema(), self._suggested()
            ),
        )

    async def async_step_measurement(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_control()
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="measurement",
            data_schema=self.add_suggested_values_to_schema(  # type: ignore[attr-defined]
                _measurement_schema(self._inverter()), self._suggested()
            ),
        )

    async def async_step_control(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            data = {**self._data, **user_input}
            if inverter_of(data) == INVERTER_SUNGROW:
                data = {k: v for k, v in data.items() if k not in FRONIUS_KEYS}
            else:
                data = {k: v for k, v in data.items() if k not in SUNGROW_ONLY_KEYS}
            return self._finish(data)
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="control",
            data_schema=self.add_suggested_values_to_schema(  # type: ignore[attr-defined]
                _control_schema(self._inverter()), self._suggested()
            ),
        )


class EmsConfigFlow(_Steps, ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    def _finish(self, data: dict[str, Any]) -> ConfigFlowResult:
        return self.async_create_entry(title=TITLE, data=data)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return await self._step_inverter("user", user_input)

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


class EmsOptionsFlow(_Steps, OptionsFlow):
    """Change the inputs later (Settings → Devices & services → Expensive Mini Savings → Configure)."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    def _base(self) -> dict[str, Any]:
        return _raw_config(self.config_entry)

    def _finish(self, data: dict[str, Any]) -> ConfigFlowResult:
        return self.async_create_entry(data=data)

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self.config_entry.source == SOURCE_IMPORT:
            # read-only: show the settings from configuration.yaml
            return self.async_abort(
                reason="yaml",
                description_placeholders={"settings": format_settings(merged_config(self.config_entry))},
            )
        return await self._step_inverter("init", user_input)
