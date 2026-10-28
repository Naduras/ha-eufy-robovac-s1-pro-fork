"""Select platform for Eufy Robovac."""
import logging
from typing import Any
import asyncio
import base64

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_COORDINATOR, CONF_DISCOVERED_DEVICES, DOMAIN

logger = logging.getLogger(__name__)

# S1 Pro Cleaning Mode definitions
CLEANING_MODES = {
    "vacuum": {
        "name": "Vacuum Only",
        "dps154": "FAoKCgASABoAIgIIAhIGCAEQASAB",
        "dps10": None
    },
    "mop_low": {
        "name": "Vacuum and Mop (Water Level: Low)",
        "dps154": "FAoKCgIIAhIAGgAiABIGCAEQASAB",
        "dps10": "low"
    },
    "mop_middle": {
        "name": "Vacuum and Mop (Water Level: Medium)",
        "dps154": "FgoMCgIIAhIAGgAiAggBEgYIARABIAE=",
        "dps10": "middle"
    },
    "mop_high": {
        "name": "Vacuum and Mop (Water Level: High)",
        "dps154": "FgoMCgIIAhIAGgAiAggCEgYIARABIAE=",
        "dps10": "high"
    }
}

# Map DPS values to mode names
DPS_TO_MODE_MAP = {
    ("FAoKCgASABoAIgIIAhIGCAEQASAB", None): "vacuum",
    ("FAoKCgIIAhIAGgAiABIGCAEQASAB", "low"): "mop_low",
    ("FgoMCgIIAhIAGgAiAggBEgYIARABIAE=", "middle"): "mop_middle",
    ("FgoMCgIIAhIAGgAiAggCEgYIARABIAE=", "high"): "mop_high",
}


def _pb_fields(data: bytes) -> dict[int, list]:
    """Minimal protobuf parser: field number -> list of values."""
    out: dict[int, list] = {}
    pos = 0

    def varint() -> int:
        nonlocal pos
        result = shift = 0
        while True:
            byte = data[pos]
            pos += 1
            result |= (byte & 0x7F) << shift
            shift += 7
            if byte < 0x80:
                return result

    while pos < len(data):
        key = varint()
        field, wire = key >> 3, key & 7
        if wire == 0:
            value = varint()
        elif wire == 2:
            length = varint()
            value = data[pos:pos + length]
            pos += length
        elif wire == 5:
            pos += 4
            continue
        elif wire == 1:
            pos += 8
            continue
        else:
            raise ValueError("unsupported wire type")
        out.setdefault(field, []).append(value)
    return out


WATER_FROM_DPS154 = {0: "low", 1: "middle", 2: "high"}


def decode_cleaning_mode(dps154: str | None, dps10: Any) -> str | None:
    """Return the CLEANING_MODES key for the robot's current settings.

    DPS 154 also changes with fan speed and other options, so it is decoded
    structurally instead of being compared to fixed strings: field 1.1 (mop
    mode) is present while mopping is on, field 1.4.1 is the water level.
    """
    if not dps154:
        return None
    try:
        raw = base64.b64decode(dps154)
        params = _pb_fields(raw[1:1 + raw[0]])
        clean_param = _pb_fields(params.get(1, [b""])[0])
    except Exception:  # noqa: BLE001 - malformed payload
        return None
    mop = clean_param.get(1, [b""])[0]
    if not mop:
        return "vacuum"
    water = dps10 if dps10 in ("low", "middle", "high") else None
    if water is None:
        try:
            level = _pb_fields(clean_param.get(4, [b""])[0]).get(1, [0])[0]
        except Exception:  # noqa: BLE001
            level = 0
        water = WATER_FROM_DPS154.get(level, "middle")
    return {"low": "mop_low", "middle": "mop_middle", "high": "mop_high"}[water]


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the select platform."""
    discovered_devices = hass.data[DOMAIN][config_entry.entry_id][CONF_DISCOVERED_DEVICES]

    logger.debug("Setting up select entities for discovered devices: %s", discovered_devices)

    entities = []
    for device_id, props in discovered_devices.items():
        entities.append(CleaningModeSelect(coordinator=props[CONF_COORDINATOR]))
    
    async_add_entities(entities)


class CleaningModeSelect(CoordinatorEntity, RestoreEntity, SelectEntity):
    """Select entity for cleaning mode.
    
    RestoreEntity を使用して再起動後もDPSが読めるまで最終値を保持します。
    """

    _attr_name = "Cleaning Mode"
    _attr_icon = "mdi:broom"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator):
        """Initialize the select entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.tuya_client.device_id}_cleaning_mode"
        self._restored_option = None
    
    async def async_added_to_hass(self) -> None:
        """Restore last known value on startup."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state and last_state.state not in (None, "unknown", "unavailable"):
            # Validate that restored value is a known option
            valid_options = [CLEANING_MODES[m]["name"] for m in CLEANING_MODES]
            if last_state.state in valid_options:
                self._restored_option = last_state.state
                logger.debug("Restored Cleaning Mode: %s", self._restored_option)
        
    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.tuya_client.device_id)},
            manufacturer="Eufy",
            name="Eufy Robovac S1 Pro",
            model="S1 Pro (T2080)",
        )

    @property
    def options(self) -> list[str]:
        """Return available options."""
        return [CLEANING_MODES[mode]["name"] for mode in CLEANING_MODES.keys()]

    @property
    def current_option(self) -> str | None:
        """Return the currently selected option, keeping the last known one."""
        data = self.coordinator.data or {}
        mode = decode_cleaning_mode(data.get("154"), data.get("10"))
        if mode in CLEANING_MODES:
            self._restored_option = CLEANING_MODES[mode]["name"]
        # Keep showing the last known mode when the robot has not (yet)
        # reported its settings, e.g. right after a restart.
        return self._restored_option

    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        # Find the mode by name
        selected_mode = None
        for mode_key, mode_config in CLEANING_MODES.items():
            if mode_config["name"] == option:
                selected_mode = mode_key
                break
        
        if not selected_mode:
            logger.error(f"Invalid cleaning mode selected: {option}")
            return
        
        mode_config = CLEANING_MODES[selected_mode]
        logger.info(f"Setting cleaning mode to: {mode_config['name']}")
        
        try:
            # Set DPS 154
            await self.coordinator.tuya_client.async_set({"154": mode_config["dps154"]})
            await asyncio.sleep(0.5)
            
            # Set DPS 10 if needed (for mopping modes)
            if mode_config["dps10"]:
                await self.coordinator.tuya_client.async_set({"10": mode_config["dps10"]})
            await asyncio.sleep(0.5)
            
            # Refresh state
            await self.coordinator.async_request_refresh()
            
            logger.info(f"Cleaning mode set to: {mode_config['name']}")
        except Exception as e:
            logger.error(f"Failed to set cleaning mode: {e}")
