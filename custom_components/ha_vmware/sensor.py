"""Sensors: cluster, hosts, VMs, datastores and scenarios."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfFrequency,
    UnitOfInformation,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HaVmwareConfigEntry
from .entity import (
    SECTION_DATASTORES,
    SECTION_HOSTS,
    SECTION_VMS,
    ClusterEntity,
    ItemEntity,
    ScenarioEntity,
    scenario_subentries,
    track_items,
)
from .link import AgentLink
from .protocol import (
    CLUSTER_DEGRADED,
    CLUSTER_OFF,
    CLUSTER_ON,
    CMD_SCENARIO_START,
    OP_CANCELLED,
    OP_COMPLETED,
    OP_FAILED,
    OP_RUNNING,
    STEPS,
)

STATE_IDLE = "idle"
SCENARIO_INACTIVE = "inactive"
SCENARIO_STARTING = "starting"
SCENARIO_STOPPING = "stopping"
SCENARIO_ACTIVE = "active"
SCENARIO_PARTIAL = "partial"
SCENARIO_FAILED = "failed"


def _percent(used: float, total: float) -> float | None:
    return round(used / total * 100, 1) if total else None


@dataclass(frozen=True, kw_only=True)
class ValueDescription(SensorEntityDescription):
    """A sensor read from one record."""

    value_fn: Callable[[dict[str, Any]], Any]


def _measure(key: str, unit: str, value_fn: Callable, **kwargs: Any) -> ValueDescription:
    return ValueDescription(
        key=key,
        translation_key=key,
        native_unit_of_measurement=unit,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=value_fn,
        **kwargs,
    )


_MHZ = {"device_class": SensorDeviceClass.FREQUENCY}
_MIB = {"device_class": SensorDeviceClass.DATA_SIZE}

HOST_SENSORS = (
    _measure("host_cpu_used", UnitOfFrequency.MEGAHERTZ, lambda i: i["cpu_used_mhz"], **_MHZ),
    _measure("host_cpu_percent", PERCENTAGE, lambda i: _percent(i["cpu_used_mhz"], i["cpu_total_mhz"])),
    _measure("host_memory_used", UnitOfInformation.MEBIBYTES, lambda i: i["mem_used_mb"], **_MIB),
    _measure("host_memory_percent", PERCENTAGE, lambda i: _percent(i["mem_used_mb"], i["mem_total_mb"])),
    ValueDescription(
        key="host_uptime",
        translation_key="host_uptime",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda i: i["uptime_s"],
    ),
    ValueDescription(
        key="host_power",
        translation_key="host_power",
        device_class=SensorDeviceClass.ENUM,
        options=["on", "off", "suspended", "unknown"],
        value_fn=lambda i: i["power"],
    ),
)

VM_SENSORS = (
    _measure("vm_cpu_used", UnitOfFrequency.MEGAHERTZ, lambda i: i["cpu_mhz"], **_MHZ),
    _measure("vm_memory_used", UnitOfInformation.MEBIBYTES, lambda i: i["mem_mb"], **_MIB),
    _measure("vm_storage", UnitOfInformation.GIBIBYTES, lambda i: i["storage_gb"], **_MIB),
    ValueDescription(
        key="vm_host",
        translation_key="vm_host",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda i: i.get("host"),
    ),
    ValueDescription(
        key="vm_ip",
        translation_key="vm_ip",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda i: i.get("ip"),
    ),
)

DATASTORE_SENSORS = (
    _measure(
        "datastore_used_percent",
        PERCENTAGE,
        lambda i: _percent(i["capacity_gb"] - i["free_gb"], i["capacity_gb"]),
    ),
    _measure("datastore_free", UnitOfInformation.GIBIBYTES, lambda i: i["free_gb"], **_MIB),
    _measure("datastore_capacity", UnitOfInformation.GIBIBYTES, lambda i: i["capacity_gb"], **_MIB),
)


def _running_vms(state: dict[str, Any]) -> int:
    return sum(1 for vm in state["vms"] if vm["power"] == "on" and not vm.get("system"))


def _user_vms(state: dict[str, Any]) -> int:
    return sum(1 for vm in state["vms"] if not vm.get("system"))


def _hosts_up(state: dict[str, Any]) -> int:
    return sum(1 for host in state["hosts"] if host["connected"])


def _cluster_cpu(state: dict[str, Any]) -> float | None:
    hosts = state["hosts"]
    return _percent(
        sum(h["cpu_used_mhz"] for h in hosts), sum(h["cpu_total_mhz"] for h in hosts)
    )


def _cluster_memory(state: dict[str, Any]) -> float | None:
    hosts = state["hosts"]
    return _percent(
        sum(h["mem_used_mb"] for h in hosts), sum(h["mem_total_mb"] for h in hosts)
    )


CLUSTER_SENSORS = (
    ValueDescription(
        key="cluster_state",
        translation_key="cluster_state",
        device_class=SensorDeviceClass.ENUM,
        options=[CLUSTER_ON, CLUSTER_DEGRADED, CLUSTER_OFF],
        value_fn=lambda s: s["cluster"],
    ),
    _measure("cluster_cpu_percent", PERCENTAGE, _cluster_cpu),
    _measure("cluster_memory_percent", PERCENTAGE, _cluster_memory),
    ValueDescription(
        key="hosts_online",
        translation_key="hosts_online",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_hosts_up,
    ),
    ValueDescription(
        key="vms_running",
        translation_key="vms_running",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_running_vms,
    ),
    ValueDescription(
        key="vms_total",
        translation_key="vms_total",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_user_vms,
    ),
)


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: HaVmwareConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the sensors."""
    link = entry.runtime_data
    async_add_entities(
        [
            *(ClusterSensor(link, description) for description in CLUSTER_SENSORS),
            OperationSensor(link),
            OperationStepSensor(link),
        ]
    )

    def sensors(section: str, descriptions: tuple[ValueDescription, ...]) -> Callable:
        return lambda item: [
            ItemSensor(link, section, item, description) for description in descriptions
        ]

    track_items(link, entry, SECTION_HOSTS, sensors(SECTION_HOSTS, HOST_SENSORS), async_add_entities)
    track_items(link, entry, SECTION_VMS, sensors(SECTION_VMS, VM_SENSORS), async_add_entities)
    track_items(link, entry, SECTION_DATASTORES, sensors(SECTION_DATASTORES, DATASTORE_SENSORS), async_add_entities)

    for subentry in scenario_subentries(entry):
        async_add_entities(
            [ScenarioStateSensor(link, subentry)],
            config_subentry_id=subentry.subentry_id,
        )


class ClusterSensor(ClusterEntity, SensorEntity):
    """A value computed from the whole state."""

    entity_description: ValueDescription

    def __init__(self, link: AgentLink, description: ValueDescription) -> None:
        """Set the description."""
        super().__init__(link, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        """Return the value."""
        return self.entity_description.value_fn(self.link.state or {})


class ItemSensor(ItemEntity, SensorEntity):
    """A value of a host, VM or datastore."""

    entity_description: ValueDescription

    def __init__(
        self,
        link: AgentLink,
        section: str,
        item: dict[str, Any],
        description: ValueDescription,
    ) -> None:
        """Set the description."""
        super().__init__(link, section, item, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        """Return the value."""
        item = self.item
        return self.entity_description.value_fn(item) if item else None


class OperationSensor(ClusterEntity, SensorEntity):
    """State of the last long-running operation, with its plan."""

    _attr_translation_key = "operation"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [STATE_IDLE, OP_RUNNING, OP_COMPLETED, OP_FAILED, OP_CANCELLED]

    def __init__(self, link: AgentLink) -> None:
        """Set the identity."""
        super().__init__(link, "operation")

    @property
    def native_value(self) -> str:
        """Return the status."""
        operation = self.link.operation
        return operation["status"] if operation else STATE_IDLE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the details and the shutdown plan."""
        operation = self.link.operation or {}
        return {
            key: operation.get(key)
            for key in (
                "kind",
                "detail",
                "error_code",
                "error",
                "dry_run",
                "plan",
                "started",
                "finished",
            )
        }


class OperationStepSensor(ClusterEntity, SensorEntity):
    """Current step of the running operation."""

    _attr_translation_key = "operation_step"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [STATE_IDLE, *STEPS]

    def __init__(self, link: AgentLink) -> None:
        """Set the identity."""
        super().__init__(link, "operation_step")

    @property
    def native_value(self) -> str:
        """Return the step."""
        return self.link.operation["step"] if self.link.operation_running else STATE_IDLE


class ScenarioStateSensor(ScenarioEntity, SensorEntity):
    """Whether a scenario is running."""

    _attr_translation_key = "scenario_state"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [
        SCENARIO_INACTIVE,
        SCENARIO_STARTING,
        SCENARIO_STOPPING,
        SCENARIO_ACTIVE,
        SCENARIO_PARTIAL,
        SCENARIO_FAILED,
    ]

    def __init__(self, link: AgentLink, subentry: ConfigSubentry) -> None:
        """Set the identity."""
        super().__init__(link, subentry, "state")

    @property
    def native_value(self) -> str:
        """Return the state."""
        operation = self.link.operation or {}
        if operation.get("scenario") == self._subentry.subentry_id:
            starting = operation.get("kind") == CMD_SCENARIO_START
            if operation.get("status") == OP_RUNNING:
                return SCENARIO_STARTING if starting else SCENARIO_STOPPING
            if operation.get("status") == OP_FAILED:
                return SCENARIO_FAILED
        status = (self.link.state or {}).get("scenarios", {}).get(self._subentry.subentry_id)
        if status is None or status["vms_on"] == 0:
            return SCENARIO_INACTIVE
        return SCENARIO_ACTIVE if status["vms_on"] == status["vms_total"] else SCENARIO_PARTIAL
