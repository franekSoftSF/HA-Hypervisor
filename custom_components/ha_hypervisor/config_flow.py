"""Config flow: connect to an agent and define scenarios."""

from __future__ import annotations

import asyncio
from typing import Any, override

from homeassistant.components import mqtt
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol

from .const import (
    AGENT_DISCOVERY_TIMEOUT,
    CONF_ADD_ANOTHER,
    CONF_AGENT_ID,
    CONF_DELAY,
    CONF_POWER_OFF_EMPTY,
    CONF_SNAPSHOT,
    CONF_SHARED_KEY,
    CONF_STEPS,
    CONF_TIMEOUT,
    CONF_VMS,
    CONF_WAIT,
    DOMAIN,
    SUBENTRY_SCENARIO,
)
from .protocol import (
    LEAF_INFO,
    PROTOCOL_VERSION,
    SNAPSHOT_NAME,
    WAIT_MODES,
    WAIT_TOOLS,
    ProtocolError,
    is_valid_agent_id,
    is_valid_key,
    topic,
    unseal,
)


async def _async_probe_agent(hass: HomeAssistant, agent_id: str, key: str) -> str | None:
    """Return an error key, or None when the agent proved it has the key."""
    if not await mqtt.async_wait_for_mqtt_client(hass):
        return "mqtt_not_ready"

    target = topic(agent_id, LEAF_INFO)
    future: asyncio.Future[str | None] = hass.loop.create_future()

    @callback
    def received(message: mqtt.ReceiveMessage) -> None:
        if future.done():
            return
        try:
            info = unseal(key, target, message.payload)
        except ProtocolError as err:
            future.set_result("protocol" if err.code == "version" else "invalid_key")
            return
        future.set_result(
            None if info.body.get("protocol") == PROTOCOL_VERSION else "protocol"
        )

    unsubscribe = await mqtt.async_subscribe(hass, target, received, qos=1)
    try:
        async with asyncio.timeout(AGENT_DISCOVERY_TIMEOUT):
            return await future
    except TimeoutError:
        return "agent_not_found"
    finally:
        unsubscribe()


class HaHypervisorConfigFlow(ConfigFlow, domain=DOMAIN):
    """Connect to one agent."""

    VERSION = 1

    @classmethod
    @callback
    @override
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Scenarios live under the agent entry."""
        return {SUBENTRY_SCENARIO: ScenarioFlow}

    def _schema(self, agent_id: str | None) -> vol.Schema:
        password = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
        return vol.Schema(
            {
                vol.Required(CONF_AGENT_ID, default=agent_id or vol.UNDEFINED): str,
                vol.Required(CONF_SHARED_KEY): password,
            }
        )

    async def _async_validate(self, user_input: dict[str, Any]) -> str | None:
        if not is_valid_agent_id(user_input[CONF_AGENT_ID]):
            return "invalid_agent_id"
        if not is_valid_key(user_input[CONF_SHARED_KEY]):
            return "invalid_key_format"
        return await _async_probe_agent(
            self.hass, user_input[CONF_AGENT_ID], user_input[CONF_SHARED_KEY]
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the agent id and the shared key."""
        errors: dict[str, str] = {}
        agent_id = None
        if user_input is not None:
            agent_id = user_input[CONF_AGENT_ID]
            await self.async_set_unique_id(agent_id)
            self._abort_if_unique_id_configured()
            if error := await self._async_validate(user_input):
                errors["base"] = error
            else:
                return self.async_create_entry(
                    title=f"Hypervisor {agent_id}", data=user_input
                )
        return self.async_show_form(
            step_id="user", data_schema=self._schema(agent_id), errors=errors
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Replace the shared key."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, **user_input}
            if error := await self._async_validate(data):
                errors["base"] = error
            else:
                return self.async_update_and_abort(entry, data=data)
        password = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema({vol.Required(CONF_SHARED_KEY): password}),
            errors=errors,
        )


class ScenarioFlow(ConfigSubentryFlow):
    """Wizard: scenario options, then one form per start group."""

    def __init__(self) -> None:
        """Start empty."""
        self._name = ""
        self._power_off = False
        self._snapshot = ""
        self._steps: list[dict[str, Any]] = []
        self._existing: list[dict[str, Any]] = []
        self._editing = False

    def _vm_options(self) -> list[SelectOptionDict] | None:
        entry = self._get_entry()
        if entry.state is not ConfigEntryState.LOADED:
            return None
        return [
            SelectOptionDict(value=vm["id"], label=vm["name"])
            for vm in entry.runtime_data.items("vms")
            if not vm.get("system") and not vm.get("protected")
        ]

    def _options_schema(self) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required("name", default=self._name or vol.UNDEFINED): str,
                vol.Required(
                    CONF_POWER_OFF_EMPTY, default=self._power_off
                ): BooleanSelector(),
                vol.Optional(
                    CONF_SNAPSHOT, description={"suggested_value": self._snapshot or None}
                ): str,
            }
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Ask for the name and options."""
        if not self._vm_options():
            return self.async_abort(reason="no_vms_known")
        errors: dict[str, str] = {}
        if user_input is not None:
            self._name = user_input["name"]
            self._power_off = user_input[CONF_POWER_OFF_EMPTY]
            if self._read_snapshot(user_input, errors):
                return await self.async_step_group()
        return self.async_show_form(
            step_id="user", data_schema=self._options_schema(), errors=errors
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Edit a scenario, starting from its current values."""
        subentry = self._get_reconfigure_subentry()
        if not self._vm_options():
            return self.async_abort(reason="no_vms_known")
        errors: dict[str, str] = {}
        if user_input is not None:
            self._name = user_input["name"]
            self._power_off = user_input[CONF_POWER_OFF_EMPTY]
            if self._read_snapshot(user_input, errors):
                self._existing = list(subentry.data[CONF_STEPS])
                self._editing = True
                return await self.async_step_group()
        else:
            self._name = subentry.title
            self._power_off = subentry.data.get(CONF_POWER_OFF_EMPTY, False)
            self._snapshot = subentry.data.get(CONF_SNAPSHOT) or ""
        return self.async_show_form(
            step_id="reconfigure", data_schema=self._options_schema(), errors=errors
        )

    def _read_snapshot(self, user_input: dict[str, Any], errors: dict[str, str]) -> bool:
        """Keep the snapshot to revert to before a start, empty for none."""
        self._snapshot = (user_input.get(CONF_SNAPSHOT) or "").strip()
        if self._snapshot and not SNAPSHOT_NAME.match(self._snapshot):
            errors[CONF_SNAPSHOT] = "invalid_snapshot_name"
            return False
        return True

    def _group_schema(self, options: list[SelectOptionDict]) -> vol.Schema:
        index = len(self._steps)
        current = self._existing[index] if index < len(self._existing) else {}
        valid = {option["value"] for option in options}
        return vol.Schema(
            {
                vol.Required(
                    CONF_VMS,
                    default=[vm for vm in current.get(CONF_VMS, []) if vm in valid],
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=options,
                        multiple=True,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(
                    CONF_WAIT, default=current.get(CONF_WAIT, WAIT_TOOLS)
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=list(WAIT_MODES),
                        translation_key="wait_mode",
                        mode=SelectSelectorMode.LIST,
                    )
                ),
                vol.Required(
                    CONF_DELAY, default=current.get(CONF_DELAY, 0)
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        max=3600,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="s",
                    )
                ),
                vol.Required(
                    CONF_TIMEOUT, default=current.get(CONF_TIMEOUT, 600)
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=10,
                        max=7200,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="s",
                    )
                ),
                vol.Required(
                    CONF_ADD_ANOTHER, default=index + 1 < len(self._existing)
                ): BooleanSelector(),
            }
        )

    async def async_step_group(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Ask for one group of VMs started together."""
        options = self._vm_options()
        if not options:
            return self.async_abort(reason="no_vms_known")

        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input[CONF_VMS]:
                errors[CONF_VMS] = "no_vms_selected"
            else:
                step = {k: v for k, v in user_input.items() if k != CONF_ADD_ANOTHER}
                self._steps.append(step)
                if user_input[CONF_ADD_ANOTHER]:
                    return await self.async_step_group()
                return self._finish()

        return self.async_show_form(
            step_id="group",
            data_schema=self._group_schema(options),
            errors=errors,
            description_placeholders={"number": str(len(self._steps) + 1)},
        )

    def _finish(self) -> SubentryFlowResult:
        data = {CONF_POWER_OFF_EMPTY: self._power_off, CONF_STEPS: self._steps}
        if self._snapshot:
            data[CONF_SNAPSHOT] = self._snapshot
        if self._editing:
            # Not the reloading variant: the entry listener already reloads.
            return self.async_update_and_abort(
                self._get_entry(),
                self._get_reconfigure_subentry(),
                title=self._name,
                data=data,
            )
        return self.async_create_entry(title=self._name, data=data)
