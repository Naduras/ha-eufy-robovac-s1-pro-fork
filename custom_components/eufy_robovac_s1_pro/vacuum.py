import logging
from typing import Any
import asyncio
import base64
from enum import Enum

from homeassistant.components.vacuum import (
    StateVacuumEntity,
    VacuumEntityFeature,
    VacuumActivity,
)

try:  # Home Assistant 2026.3+: native area cleaning (vacuum.clean_area)
    from homeassistant.components.vacuum import Segment
except ImportError:  # older Home Assistant
    Segment = None
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util
import datetime
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_COORDINATOR, CONF_DISCOVERED_DEVICES, DOMAIN

logger = logging.getLogger(__name__)

# --- Room cleaning (eufy "Novel" protocol, DPS 152 ModeCtrlRequest) ---------
# Room IDs are the IDs the eufy app assigns to the rooms on the saved map.
# They are not reported locally, so they are offered as generic segments
# "Raum 1" ... "Raum N" which are mapped to HA areas in the entity settings.
ROOM_SEGMENT_COUNT = 12
# Known eufy room IDs on the saved map (from the app's schedules); others show as "Raum N".
ROOM_NAMES = {0: "Bad", 1: "Kinderzimmer", 2: "Flur", 3: "Wohnzimmer", 4: "Küche"}
MODE_CTRL_START_SELECT_ROOMS_CLEAN = 1


def _pb_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _pb_uint(field: int, value: int) -> bytes:
    return _pb_varint(field << 3) + _pb_varint(value)


def _pb_bytes(field: int, value: bytes) -> bytes:
    return _pb_varint((field << 3) | 2) + _pb_varint(len(value)) + value


def _pb_frame(message: bytes) -> str:
    return base64.b64encode(_pb_varint(len(message)) + message).decode()


# The S1 Pro (T2080A) ignores the room list of a GENERAL room clean and only
# accepts its *local* map id (1 for the first saved map, not the cloud id).
# Working sequence (as the eufy app does it):
#   1. DPS 170 MapEditRequest SET_ROOMS_CUSTOM with map_id + rooms
#   2. DPS 152 ModeCtrlRequest START_SELECT_ROOMS_CLEAN, mode CUSTOMIZE
MAP_EDIT_SET_ROOMS_CUSTOM = 5
SELECT_ROOMS_MODE_CUSTOMIZE = 1
DEFAULT_LOCAL_MAP_ID = 1


def build_room_custom_command(room_ids: list[int], map_id: int, clean_times: int = 1) -> str:
    """DPS 170 payload: MapEditRequest(SET_ROOMS_CUSTOM) for the given rooms."""
    rooms = b"".join(
        _pb_bytes(1, _pb_uint(1, room_id) + _pb_bytes(2, _pb_uint(5, clean_times)))
        for room_id in room_ids
    )
    rooms_custom = _pb_bytes(3, rooms)  # RoomsCustom.rooms_parm
    request = (
        _pb_uint(1, MAP_EDIT_SET_ROOMS_CUSTOM)
        + _pb_uint(3, map_id)
        + _pb_bytes(10, rooms_custom)
    )
    return _pb_frame(request)


def build_room_clean_command(
    room_ids: list[int],
    clean_times: int = 1,
    map_id: int | None = DEFAULT_LOCAL_MAP_ID,
    customize: bool = False,
) -> str:
    """DPS 152 payload: START_SELECT_ROOMS_CLEAN for the given rooms in order.

    The S1 Pro (T2080A) numbers the room order from 0. With orders starting
    at 1 it ignores the list and cleans room 0 / the last app selection
    (cf. jeppesens/eufy-clean#182). proto3 zero values are left out.
    """
    def uint(field: int, value: int) -> bytes:
        return _pb_uint(field, value) if value else b""

    rooms = b"".join(
        _pb_bytes(1, uint(1, room_id) + uint(2, order))
        for order, room_id in enumerate(room_ids)
    )
    select_rooms = rooms + uint(2, clean_times)
    if map_id is not None:
        select_rooms += uint(3, map_id)
    if customize:
        select_rooms += uint(5, SELECT_ROOMS_MODE_CUSTOMIZE)
    request = _pb_uint(1, MODE_CTRL_START_SELECT_ROOMS_CLEAN) + _pb_bytes(4, select_rooms)
    return _pb_frame(request)


def parse_mode_ctrl_result(value: str | None) -> tuple[int, int] | None:
    """Parse a DPS 152 ModeCtrlResponse into (method, result); result 1 = FAILED."""
    if not value:
        return None
    try:
        raw = base64.b64decode(value)
        fields = _pb_parse(raw[1:1 + raw[0]])
        return _pb_get(fields, 1, 0), _pb_get(fields, 3, 0)
    except Exception:  # noqa: BLE001
        return None


# --- Room cleaning via one-shot timer (DPS 164) -------------------------------
# Fallback only: used when the immediate room clean is rejected or a different
# cleaning mode is requested. Room cleaning from a *schedule* is
# executed correctly, so a single-shot timer for the next minute is created and
# deleted again once it has fired. See tkoba1974/ha-eufy-robovac-s1-pro#24.
TIMER_DPS = "164"
TIMER_ADD = 1
TIMER_DELETE = 2
TIMER_INQUIRY = 6
ACTION_SCHEDULE_ROOMS_CLEAN = 1

FAN_TO_LEVEL = {"Quiet": 0, "gentle": 0, "Standard": 1, "normal": 1, "middle": 1,
                "Turbo": 2, "strong": 2, "Max": 3, "max": 3, "Maximum": 3}
WATER_TO_LEVEL = {"low": 0, "middle": 1, "high": 2}
CLEAN_TYPE = {"vacuum": 0, "mop": 1, "vacuum_mop": 2}
# DPS 154 payloads of the app's cleaning modes (same as select.py), keyed by water level
CLEAN_MODE_DPS154 = {
    "vacuum": "FAoKCgASABoAIgIIAhIGCAEQASAB",
    "low": "FAoKCgIIAhIAGgAiABIGCAEQASAB",
    "middle": "FgoMCgIIAhIAGgAiAggBEgYIARABIAE=",
    "high": "FgoMCgIIAhIAGgAiAggCEgYIARABIAE=",
}


def build_timer_rooms_clean(
    room_ids: list[int],
    hour: int,
    minute: int,
    seq: int,
    map_id: int = DEFAULT_LOCAL_MAP_ID,
    fan: int = 0,
    water: int = 0,
    clean_type: int = 2,
    clean_times: int = 1,
) -> str:
    """DPS 164 payload: TimerRequest(ADD) for a single room clean at hour:minute."""
    general = _pb_uint(1, map_id)
    general += _pb_bytes(2, _pb_uint(1, fan) if fan else b"")
    general += _pb_bytes(3, _pb_uint(1, water) if water else b"")
    general += _pb_bytes(4, _pb_uint(1, clean_type) if clean_type else b"")
    general += _pb_bytes(5, b"")
    for order, room_id in enumerate(room_ids):
        room = (_pb_uint(1, room_id) if room_id else b"") + (_pb_uint(2, order) if order else b"")
        general += _pb_bytes(6, room)
    if clean_times:
        general += _pb_uint(17, clean_times)
    action = _pb_uint(1, ACTION_SCHEDULE_ROOMS_CLEAN) + _pb_bytes(4, _pb_bytes(2, general))
    timing = _pb_uint(2, 1) + (_pb_uint(3, hour) if hour else b"") + (_pb_uint(4, minute) if minute else b"")
    desc = _pb_bytes(2, timing)  # trigger SINGLE (0), no cycle
    status = _pb_uint(1, 1) + _pb_uint(2, 1)  # valid, opened
    info = _pb_bytes(2, status) + _pb_bytes(3, desc) + _pb_bytes(5, action)
    request = _pb_uint(1, TIMER_ADD) + _pb_uint(2, seq) + _pb_bytes(3, info)
    return _pb_frame(request)


def build_timer_delete(timer_id: int, seq: int) -> str:
    """DPS 164 payload: TimerRequest(DELETE) for the given timer id."""
    request = _pb_uint(1, TIMER_DELETE) + _pb_uint(2, seq) + _pb_bytes(3, _pb_bytes(1, _pb_uint(1, timer_id)))
    return _pb_frame(request)


def _pb_parse(data: bytes) -> list[tuple[int, int, Any]]:
    """Minimal protobuf parser: list of (field, wire_type, value)."""
    out: list[tuple[int, int, Any]] = []
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
            out.append((field, wire, varint()))
        elif wire == 2:
            length = varint()
            out.append((field, wire, data[pos:pos + length]))
            pos += length
        elif wire == 5:
            pos += 4
        elif wire == 1:
            pos += 8
        else:
            raise ValueError("unsupported wire type")
    return out


def _pb_get(fields: list[tuple[int, int, Any]], number: int, default: Any = None) -> Any:
    for field, _, value in fields:
        if field == number:
            return value
    return default


def parse_timer_response(value: str) -> tuple[int, int, list[dict[str, Any]]] | None:
    """Parse a DPS 164 TimerResponse into (method, seq, timers)."""
    try:
        raw = base64.b64decode(value)
        pos = 0
        length = shift = 0
        while True:
            byte = raw[pos]
            pos += 1
            length |= (byte & 0x7F) << shift
            shift += 7
            if byte < 0x80:
                break
        fields = _pb_parse(raw[pos:pos + length])
        timers = []
        for field, _, info_raw in fields:
            if field != 4:
                continue
            info = _pb_parse(info_raw)
            timer_id = _pb_get(_pb_parse(_pb_get(info, 1, b"")), 1, 0)
            desc = _pb_parse(_pb_get(info, 3, b""))
            timing = _pb_parse(_pb_get(desc, 2, b""))
            timers.append({
                "id": timer_id,
                "single": _pb_get(desc, 3) is None and not _pb_get(desc, 1, 0),
                "hour": _pb_get(timing, 3, 0),
                "minute": _pb_get(timing, 4, 0),
            })
        return _pb_get(fields, 1, 0), _pb_get(fields, 2, 0), timers
    except Exception:  # noqa: BLE001 - malformed or unexpected payload
        return None


# S1 Pro actual fan speed mappings (from app testing)
HA_TO_EUFY_FAN_SPEED_MAP = {
    "Quiet": ("gentle", "Quiet"),      # DPS 9: gentle, DPS 158: Quiet
    "Standard": ("normal", "Standard"), # DPS 9: normal, DPS 158: Standard  
    "Turbo": ("strong", "Turbo"),      # DPS 9: strong, DPS 158: Turbo
    "Maximum": ("max", "Max")           # DPS 9: max, DPS 158: Max
}

# Reverse mapping for display
EUFY_TO_HA_FAN_SPEED_MAP = {
    "gentle": "Quiet",
    "normal": "Standard",
    "strong": "Turbo",
    "max": "Maximum",
    "Quiet": "Quiet",
    "Standard": "Standard",
    "Turbo": "Turbo",
    "Max": "Maximum",
    "middle": "Standard",  # Fallback
}

# S1 Pro Command definitions for DPS 152 (from actual app logs)
S1_PRO_COMMANDS = {
    "start": "AA==",        # 掃除開始
    "cleaning": "AggO",     # 掃除中
    "pause": "AggN",        # 一時停止
    "return": "AggG",       # ステーション帰還
}


class RobovacState(Enum):
    """ロボット掃除機の状態定義"""
    CLEANING = "cleaning"
    PAUSED = "paused"
    RETURNING = "returning"
    DOCKED = "docked"
    ERROR = "error"
    UNKNOWN = "unknown"


def _decode_work_status(decoded: bytes) -> tuple[RobovacState, str] | None:
    """Decode a length-prefixed WorkStatus message (DPS 153)."""
    try:
        length, pos, shift = 0, 0, 0
        while True:
            byte = decoded[pos]
            pos += 1
            length |= (byte & 0x7F) << shift
            shift += 7
            if byte < 0x80:
                break
        fields = _pb_parse(decoded[pos:pos + length])
    except Exception:  # noqa: BLE001
        return None
    state = _pb_get(fields, 2, 0)
    if state == 5:  # CLEANING
        cleaning = _pb_parse(_pb_get(fields, 6, b"") or b"")
        go_wash = _pb_get(fields, 7)
        if go_wash is not None:
            return RobovacState.CLEANING, "washing_mop"
        if _pb_get(cleaning, 1, 0) == 1:
            return RobovacState.PAUSED, "paused"
        return RobovacState.CLEANING, "cleaning"
    if state == 7:  # GO_HOME
        go_home = _pb_parse(_pb_get(fields, 8, b"") or b"")
        if _pb_get(go_home, 1, 0) == 1:
            return RobovacState.PAUSED, "paused"
        return RobovacState.RETURNING, "returning"
    if state == 2:  # FAULT
        return RobovacState.ERROR, "error"
    if state in (4, 6, 8):  # mapping, remote control, cruising
        return RobovacState.CLEANING, "cleaning"
    if state in (0, 1, 3, 9):  # standby, sleep, charging, station work
        return None  # let the existing docked sub-status logic decide
    return None


def decode_dps153_to_state(dps153_value: str) -> tuple[RobovacState, str]:
    """
    dps153の値からロボット掃除機の状態とサブステータスを判定
    
    この関数は環境や設定の違いに対応できるよう、バイトパターンの
    普遍的な特徴に基づいて判定を行います。
    
    判定ロジック:
    1. Cleaning: Byte[1]=0x0a, Byte[2]=0x00, Byte[3]=0x10, Byte[4]=0x05, length=7
    2. Paused: Byte[1]=0x0a, Byte[2]=0x00, Byte[3]=0x10, Byte[4]=0x05, length>=9, Byte[6]=0x02
    3. Returning: Byte[1]=0x10, Byte[2]=0x07, Byte[3]=0x42
    4. Docked: 上記以外の場合
    
    Args:
        dps153_value: Base64エンコードされたdps153の値、またはバイト列
        
    Returns:
        (RobovacState, substatus_str): 判定された状態とサブステータス文字列のタプル
    """
    try:
        # Base64文字列の場合はデコード
        if isinstance(dps153_value, str):
            decoded = base64.b64decode(dps153_value)
        else:
            decoded = dps153_value
        
        # 最低限の長さチェック
        if len(decoded) < 3:
            logger.warning(f"dps153 data too short: {len(decoded)} bytes")
            return RobovacState.UNKNOWN, "unknown"
        
        byte1 = decoded[1]
        byte2 = decoded[2]
        
        # デバッグログ
        hex_str = ' '.join([f"{b:02x}" for b in decoded])
        logger.debug(f"dps153 decoded: {hex_str}")
        
        # ========== 主要な状態判定 ==========
        
        # Byte[1]=0x0a, Byte[2]=0x00 のパターン
        # (Cleaning, Paused, モップ関連Docked)
        if byte1 == 0x0a and byte2 == 0x00:
            if len(decoded) >= 5:
                byte3 = decoded[3]
                byte4 = decoded[4]
                
                # Cleaning/Pausedのパターン
                if byte3 == 0x10 and byte4 == 0x05:
                    # Pausedの判定
                    if len(decoded) >= 7 and decoded[6] == 0x02:
                        return RobovacState.PAUSED, "paused"
                    else:
                        return RobovacState.CLEANING, "cleaning"
                
                # モップ関連Dockedのパターン
                elif byte3 == 0x10 and byte4 == 0x09:
                    substatus = _get_docked_substatus(decoded)
                    return RobovacState.DOCKED, substatus
        
        # Returning の判定
        if byte1 == 0x10 and byte2 == 0x07:
            if len(decoded) >= 4 and decoded[3] == 0x42:
                return RobovacState.RETURNING, "returning"
        
        # Docked (その他) の判定
        if byte1 == 0x10:
            substatus = _get_docked_substatus(decoded)
            return RobovacState.DOCKED, substatus
        
        # Fallback: decode the WorkStatus protobuf properly (e.g. room cleaning,
        # where the mode field is set and the byte patterns above do not match).
        parsed = _decode_work_status(decoded)
        if parsed is not None:
            return parsed

        # デフォルトはDocked (未知のパターンでも安全側に倒す)
        logger.debug(f"Unknown dps153 pattern, defaulting to DOCKED: {hex_str}")
        return RobovacState.DOCKED, "idle"
        
    except Exception as e:
        logger.error(f"Error decoding dps153: {e}", exc_info=True)
        return RobovacState.UNKNOWN, "error"


def _get_docked_substatus(decoded: bytes) -> str:
    """
    Docked状態の詳細なサブステータスを取得
    
    Args:
        decoded: デコードされたdps153のバイト列
        
    Returns:
        サブステータス文字列
    """
    if len(decoded) < 3:
        return "unknown"
    
    byte1 = decoded[1]
    byte2 = decoded[2]
    
    # Byte[1]=0x10 の場合
    if byte1 == 0x10:
        if byte2 == 0x03:
            # 充電関連
            if len(decoded) >= 5:
                if decoded[4] == 0x00:
                    return "charging"
                elif decoded[4] == 0x02:
                    return "fully_charged"
            return "charging"
        
        elif byte2 == 0x09:
            # モップ関連操作
            if len(decoded) >= 4:
                byte3 = decoded[3]
                
                if byte3 == 0xfa:
                    return "dust_collecting"
                elif byte3 == 0x1a:
                    return "mop_drying"
                elif byte3 == 0x3a:
                    return "mop_washing"
            
            return "mop_operations"
    
    # Byte[1]=0x0a の場合 (給水中、掃除前モップ洗浄中など)
    if byte1 == 0x0a and byte2 == 0x00:
        if len(decoded) >= 5 and decoded[3] == 0x10 and decoded[4] == 0x09:
            if len(decoded) >= 12 and decoded[11] == 0x3a:
                return "mop_washing_pre"
            return "water_refilling"
    
    return "idle"


# サブステータスの人間が読める説明文
SUBSTATUS_DESCRIPTIONS = {
    "charging": "Charging",
    "fully_charged": "Fully Charged",
    "dust_collecting": "Collecting Dust",
    "water_refilling": "Refilling Water",
    "mop_washing_pre": "Pre-washing Mop",
    "mop_washing": "Washing Mop",
    "mop_drying": "Drying Mop",
    "mop_operations": "Mop Operations",
    "cleaning": "Cleaning",
    "paused": "Paused",
    "returning": "Returning to Dock",
    "idle": "Idle",
    "unknown": "Unknown",
    "error": "Error",
}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_devices: AddEntitiesCallback,
) -> None:
    discovered_devices = hass.data[DOMAIN][config_entry.entry_id][CONF_DISCOVERED_DEVICES]

    logger.debug("Got discovered devices: %s", discovered_devices)

    return async_add_devices(
        [RobovacVacuum(coordinator=props[CONF_COORDINATOR]) for device_id, props in discovered_devices.items()]
    )


class RobovacVacuum(CoordinatorEntity, StateVacuumEntity):

    _attr_name = "Eufy Robovac S1 Pro"
    _attr_supported_features = (
        VacuumEntityFeature.PAUSE
        | VacuumEntityFeature.RETURN_HOME
        | VacuumEntityFeature.START
        | VacuumEntityFeature.STATE
        | VacuumEntityFeature.FAN_SPEED
        | VacuumEntityFeature.SEND_COMMAND
    )
    if hasattr(VacuumEntityFeature, "BATTERY"):  # removed in newer Home Assistant
        _attr_supported_features |= VacuumEntityFeature.BATTERY
    if Segment is not None:
        _attr_supported_features |= VacuumEntityFeature.CLEAN_AREA

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._last_command = None
        self._last_command_time = 0
        self._was_paused = False  # 一時停止状態を記憶
        self._substatus = None  # サブステータスを保持
        self._detected_state = None  # 判定された状態を保持

    @property
    def icon(self) -> str:
        if self.activity == VacuumActivity.ERROR:
            return "mdi:robot-vacuum-alert"
        return "mdi:robot-vacuum"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.unique_id)},
            manufacturer="Eufy",
            name=self.name,
            model="S1 Pro (T2080)",
        )

    @property
    def unique_id(self) -> str:
        return self.coordinator.tuya_client.device_id

    @property
    def activity(self) -> VacuumActivity | None:
        """Return the current activity of the vacuum."""
        if not self.coordinator.data:
            return None
            
        # S1 Pro status detection based on actual DPS values
        dps6 = self.coordinator.data.get("6", 0)      # Status indicator 1
        dps153 = self.coordinator.data.get("153", "")  # Actual status indicator (most reliable)
        
        logger.debug(f"Activity check - DPS 6: {dps6}, DPS 153: {dps153}")
        
        # Error detection
        if isinstance(dps6, int) and dps6 >= 100:
            self._detected_state = RobovacState.ERROR
            self._substatus = "error"
            return VacuumActivity.ERROR
        
        # Check DPS 153 status using improved pattern-based detection
        if dps153:
            detected_state, substatus = decode_dps153_to_state(dps153)
            
            # 判定結果を保持
            self._detected_state = detected_state
            self._substatus = substatus
            
            logger.debug(f"Detected state: {detected_state.value}, substatus: {substatus}")
            
            # 状態に応じたフラグ更新と値の返却
            if detected_state == RobovacState.CLEANING:
                self._was_paused = False
                return VacuumActivity.CLEANING
            elif detected_state == RobovacState.PAUSED:
                self._was_paused = True
                return VacuumActivity.PAUSED
            elif detected_state == RobovacState.RETURNING:
                self._was_paused = False
                return VacuumActivity.RETURNING
            elif detected_state == RobovacState.DOCKED:
                self._was_paused = False
                return VacuumActivity.DOCKED
            elif detected_state == RobovacState.ERROR:
                return VacuumActivity.ERROR
            else:  # UNKNOWN
                # 未知の状態はIDLEとして扱う
                return VacuumActivity.IDLE
        
        # DPS 153が利用できない場合のフォールバック
        # (互換性のために旧ロジックを一部残す)
        dps152 = self.coordinator.data.get("152", "")
        dps6 = self.coordinator.data.get("6", 0)
        dps7 = self.coordinator.data.get("7", 0)
        
        logger.debug(f"Fallback to DPS 152/6/7 - DPS 152: {dps152}, DPS 6: {dps6}, DPS 7: {dps7}")
        
        if dps152 == S1_PRO_COMMANDS["cleaning"] or dps152 == "AggO":
            self._was_paused = False
            return VacuumActivity.CLEANING
        elif dps152 == S1_PRO_COMMANDS["pause"] or dps152 == "AggN":
            self._was_paused = True
            return VacuumActivity.PAUSED
        elif dps152 == S1_PRO_COMMANDS["return"] or dps152 == "AggG":
            self._was_paused = False
            return VacuumActivity.RETURNING
        
        # Without DPS 153 (e.g. right after a restart, the robot only publishes it
        # on the next state change) DPS 6/7 are not a reliable status source:
        # observed values while docked include 3/4, 3/2, 0/0 and 26/17. Report
        # DOCKED, which is where the robot spends nearly all of its idle time;
        # the next DPS 153 update corrects it immediately.
        self._was_paused = False
        return VacuumActivity.DOCKED

    @property
    def battery_level(self) -> int | None:
        """Returns the battery level as a percentage"""
        if self.coordinator.data:
            # S1 Pro uses DPS 8 for battery level (confirmed from logs)
            value = self.coordinator.data.get("8")
            if value is not None:
                try:
                    battery = int(value)
                    if 0 <= battery <= 100:
                        return battery
                except (ValueError, TypeError):
                    pass
            
            # Fallback to DPS 163
            value = self.coordinator.data.get("163")
            if value is not None:
                try:
                    battery = int(value)
                    if 0 <= battery <= 100:
                        return battery
                except (ValueError, TypeError):
                    pass
        return None

    @property
    def state_attributes(self) -> dict[str, Any]:
        """Return the state attributes of the vacuum."""
        attrs = super().state_attributes or {}
        
        if self.coordinator.data:
            # Only include essential attributes for end users
            if error_code := self.error_code:
                attrs["error_code"] = error_code
            
        return attrs
    
    def _is_running(self) -> bool:
        """Check if vacuum is actually running based on multiple indicators."""
        if self.coordinator.data:
            dps153 = self.coordinator.data.get("153", "")
            
            # Check DPS 153 first (most reliable)
            if dps153:
                detected_state, _ = decode_dps153_to_state(dps153)
                return detected_state == RobovacState.CLEANING
            
            # Fallback to DPS 152 if DPS 153 is not available
            dps152 = self.coordinator.data.get("152", "")
            if dps152 == S1_PRO_COMMANDS["cleaning"] or dps152 == "AggO":
                return True
            
            # Final fallback to DPS 6/7
            dps6 = self.coordinator.data.get("6", 0)
            dps7 = self.coordinator.data.get("7", 0)
            return (dps6 == 2 and dps7 == 3)
                
        return False

    @property
    def error_code(self) -> str | None:
        """Return error code if any."""
        if self.coordinator.data:
            # Check if DPS 6 has an error value (high numbers)
            error_code = self.coordinator.data.get("6")
            if isinstance(error_code, int) and error_code >= 100:
                return str(error_code)
        return None

    @property
    def fan_speed(self) -> str | None:
        """Return the current fan speed."""
        if self.coordinator.data:
            # Check DPS 9 first (primary)
            raw_speed = self.coordinator.data.get("9")
            if raw_speed and raw_speed in EUFY_TO_HA_FAN_SPEED_MAP:
                return EUFY_TO_HA_FAN_SPEED_MAP[raw_speed]
            
            # Check DPS 158 as fallback
            raw_speed = self.coordinator.data.get("158")
            if raw_speed and raw_speed in EUFY_TO_HA_FAN_SPEED_MAP:
                return EUFY_TO_HA_FAN_SPEED_MAP[raw_speed]
        return None

    @property
    def fan_speed_list(self) -> list[str]:
        """Get the list of available fan speeds."""
        return ["Quiet", "Standard", "Turbo", "Maximum"]

    async def _send_command(self, command: str) -> None:
        """Send command via DPS 152."""
        try:
            logger.info(f"Sending command via DPS 152: {command}")
            
            # Store last command for debugging
            self._last_command = command
            self._last_command_time = asyncio.get_event_loop().time()
            
            # Send command to DPS 152
            await self.coordinator.tuya_client.async_set({"152": command})
            
            # Wait for response
            await asyncio.sleep(0.5)
            
            # Also set appropriate mode for consistency
            if command == S1_PRO_COMMANDS["start"]:
                await self.coordinator.tuya_client.async_set({"5": "smart"})
            elif command == S1_PRO_COMMANDS["pause"]:
                await self.coordinator.tuya_client.async_set({"5": "pause"})
            elif command == S1_PRO_COMMANDS["return"]:
                await self.coordinator.tuya_client.async_set({"5": "charge"})
            
            # Wait and refresh state
            await asyncio.sleep(1.0)
            await self.coordinator.async_request_refresh()
            
        except Exception as e:
            logger.error(f"Failed to send command: {e}")
            raise

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the vacuum on and start cleaning."""
        logger.info("Starting vacuum cleaning via DPS 152")
        
        try:
            # Clear pause state
            self._was_paused = False
            
            # Send start command
            await self._send_command(S1_PRO_COMMANDS["start"])
            
            # Wait for state to stabilize
            await asyncio.sleep(2.0)
            
            # Send cleaning command to confirm
            await self._send_command(S1_PRO_COMMANDS["cleaning"])
            
            # Final refresh
            await self.coordinator.async_request_refresh()
            
            if self._is_running():
                logger.info("Vacuum started successfully")
            else:
                logger.warning("Vacuum may not have started properly")
                
        except Exception as e:
            logger.error(f"Failed to start vacuum: {e}")
            raise

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the vacuum off."""
        logger.info("Stopping vacuum")
        await self.async_pause()  # For S1 Pro, stop = pause

    async def async_start(self) -> None:
        """Start or resume cleaning."""
        logger.debug("Starting/resuming cleaning")
        
        # Check current state
        activity = self.activity
        current_dps152 = self.coordinator.data.get("152", "")
        current_dps153 = self.coordinator.data.get("153", "")
        
        # dps153の判定を新しいロジックで行う
        is_paused_by_dps153 = False
        if current_dps153:
            detected_state, _ = decode_dps153_to_state(current_dps153)
            is_paused_by_dps153 = (detected_state == RobovacState.PAUSED)
        
        # 一時停止状態からの再開か確認
        if (activity == VacuumActivity.PAUSED or 
            self._was_paused or 
            is_paused_by_dps153 or
            current_dps152 == S1_PRO_COMMANDS["pause"]):
            # 一時停止からの再開 - cleaningコマンドのみ送信
            logger.info("Resuming from pause - sending cleaning command only")
            
            # 掃除中コマンドを直接送信（「掃除を再開」のアナウンス）
            await self._send_command(S1_PRO_COMMANDS["cleaning"])
            
            # 一時停止フラグをクリア
            self._was_paused = False
            
        else:
            # 新規開始（「掃除を開始」のアナウンス）
            logger.info("Starting new cleaning session")
            await self.async_turn_on()

    async def async_pause(self) -> None:
        """Pause the vacuum."""
        logger.debug("Pausing vacuum via DPS 152")
        
        try:
            # 一時停止状態を記憶
            self._was_paused = True
            
            # Send pause command
            await self._send_command(S1_PRO_COMMANDS["pause"])
            
            logger.info("Vacuum paused")
        except Exception as e:
            logger.error(f"Failed to pause vacuum: {e}")
            self._was_paused = False  # エラー時はリセット

    async def async_stop(self, **kwargs: Any) -> None:
        """Stop the vacuum - S1 Pro doesn't have stop, using pause instead."""
        logger.debug("Stop requested - using pause for S1 Pro")
        await self.async_pause()

    async def async_return_to_base(self, **kwargs: Any) -> None:
        """Return vacuum to base."""
        logger.debug("Returning to base via DPS 152")
        
        try:
            # Clear pause state
            self._was_paused = False
            
            # Send return command
            await self._send_command(S1_PRO_COMMANDS["return"])
            
            logger.info("Return to base command sent")
        except Exception as e:
            logger.error(f"Failed to return to base: {e}")

    async def async_clean_spot(self, **kwargs: Any) -> None:
        """Perform a spot clean-up - Not supported on S1 Pro."""
        logger.info("Spot cleaning is not supported on S1 Pro - ignoring request")
        # Do nothing, but don't raise an error for compatibility

    async def async_locate(self, **kwargs: Any) -> None:
        """Locate the vacuum (make it beep) - Not supported on S1 Pro."""
        logger.info("Locate function is not supported on S1 Pro - ignoring request")
        # Do nothing, but don't raise an error for compatibility

    async def async_set_fan_speed(self, fan_speed: str, **kwargs: Any) -> None:
        """Set the vacuum's fan speed."""
        if fan_speed not in HA_TO_EUFY_FAN_SPEED_MAP:
            logger.error(f"Invalid fan speed: {fan_speed}")
            return
            
        logger.debug(f"Setting fan speed to {fan_speed}")
        
        try:
            dps9_value, dps158_value = HA_TO_EUFY_FAN_SPEED_MAP[fan_speed]
            
            # Set both DPS values for S1 Pro
            await self.coordinator.tuya_client.async_set({
                "9": dps9_value,
                "158": dps158_value
            })
            
            logger.info(f"Fan speed set to {fan_speed}")
        except Exception as e:
            logger.error(f"Failed to set fan speed: {e}")

    def _current_clean_settings(self) -> tuple[int, int, int]:
        """Mirror the robot's current fan speed, water level and mop state."""
        dps = self.coordinator.data or {}
        fan = FAN_TO_LEVEL.get(dps.get("158"), FAN_TO_LEVEL.get(dps.get("9"), 0))
        water = WATER_TO_LEVEL.get(dps.get("10"), 0)
        clean_type = CLEAN_TYPE["vacuum"]
        try:  # DPS 154 field 1.1 is set while mopping is enabled (cleaning mode select)
            raw = base64.b64decode(dps.get("154") or "")
            params = _pb_parse(raw[1:1 + raw[0]]) if raw else []
            mop = _pb_parse(_pb_get(_pb_parse(_pb_get(params, 1, b"")), 1, b""))
            if mop:
                clean_type = CLEAN_TYPE["vacuum_mop"]
        except Exception:  # noqa: BLE001
            if dps.get("40") == "installed":
                clean_type = CLEAN_TYPE["vacuum_mop"]
        return fan, water, clean_type

    async def async_clean_rooms(
        self,
        room_ids: list[int],
        clean_times: int = 1,
        map_id: int | None = None,
        clean_mode: str | None = None,
    ) -> None:
        """Clean the given eufy room IDs.

        Starts immediately (DPS 152, zero-based room order). When another
        cleaning mode is requested, the robot's mode is switched first
        (DPS 154/10). Falls back to a one-shot schedule for the next minute
        when the robot rejects the immediate command or the mode cannot be
        switched (schedules carry their own mode).
        """
        if not room_ids:
            logger.warning("Room clean requested without rooms - ignoring")
            return
        map_id = DEFAULT_LOCAL_MAP_ID if map_id is None else map_id
        fan, water, clean_type = self._current_clean_settings()
        wanted_type = CLEAN_TYPE.get(clean_mode, clean_type) if clean_mode else clean_type
        self._was_paused = False

        if wanted_type != clean_type and await self._async_set_clean_type(wanted_type):
            clean_type = wanted_type
        if wanted_type == clean_type and await self._async_clean_rooms_now(room_ids, clean_times, map_id):
            return
        await self._async_clean_rooms_scheduled(
            room_ids, clean_times, map_id, fan, water, wanted_type
        )

    async def _async_set_clean_type(self, clean_type: int) -> bool:
        """Switch the robot's cleaning mode (DPS 154/10) so a room clean can start at once."""
        dps = self.coordinator.data or {}
        if clean_type == CLEAN_TYPE["vacuum"]:
            dps154, dps10 = CLEAN_MODE_DPS154["vacuum"], None
        elif clean_type == CLEAN_TYPE["vacuum_mop"]:
            dps10 = dps.get("10") if dps.get("10") in CLEAN_MODE_DPS154 else "middle"
            dps154 = CLEAN_MODE_DPS154[dps10]
        else:
            return False
        logger.info("Switching cleaning mode to type %s before room clean (DPS 154: %s)", clean_type, dps154)
        try:
            await self.coordinator.tuya_client.async_set({"154": dps154})
            if dps10:
                await asyncio.sleep(0.5)
                await self.coordinator.tuya_client.async_set({"10": dps10})
            await asyncio.sleep(1)
        except Exception as err:  # noqa: BLE001
            logger.warning("Could not switch cleaning mode: %s", err)
            return False
        if self.coordinator.data is not None:
            self.coordinator.data["154"] = dps154
            if dps10:
                self.coordinator.data["10"] = dps10
        return True

    async def _async_clean_rooms_now(
        self, room_ids: list[int], clean_times: int, map_id: int
    ) -> bool:
        """Start a room clean immediately; return False if the robot rejected it."""
        payload = build_room_clean_command(room_ids, clean_times, map_id)
        logger.info("Room clean for %s now (map_id=%s) via DPS 152: %s", room_ids, map_id, payload)
        if self.coordinator.data is not None:
            self.coordinator.data.pop("152", None)
        await self.coordinator.tuya_client.async_set({"152": payload})
        for _ in range(10):
            await asyncio.sleep(0.5)
            reply = parse_mode_ctrl_result((self.coordinator.data or {}).get("152"))
            if reply and reply[0] == MODE_CTRL_START_SELECT_ROOMS_CLEAN:
                if reply[1] == 1:
                    logger.warning("Robot rejected immediate room clean, using a schedule instead")
                    return False
                break
        await self.coordinator.async_request_refresh()
        return True

    async def _async_clean_rooms_scheduled(
        self,
        room_ids: list[int],
        clean_times: int,
        map_id: int,
        fan: int,
        water: int,
        clean_type: int,
    ) -> None:
        """Clean the given rooms via a one-shot schedule for the next minute."""
        now = dt_util.now()
        start = (now + datetime.timedelta(minutes=2 if now.second >= 45 else 1)).replace(second=0, microsecond=0)
        self._timer_seq = (getattr(self, "_timer_seq", 400) + 1) % 60000 or 1
        seq = self._timer_seq
        payload = build_timer_rooms_clean(
            room_ids, start.hour, start.minute, seq, map_id, fan, water, clean_type, clean_times
        )
        self._pending_timers = getattr(self, "_pending_timers", {})
        self._pending_timers[seq] = (start.hour, start.minute, start)
        logger.info(
            "Room clean for %s scheduled at %s (map_id=%s, fan=%s, water=%s, type=%s) via DPS 164: %s",
            room_ids, start.strftime("%H:%M"), map_id, fan, water, clean_type, payload,
        )
        await self.coordinator.tuya_client.async_set({TIMER_DPS: payload})

    @callback
    def _handle_coordinator_update(self) -> None:
        """Track our one-shot timers and delete them after they have fired."""
        pending = getattr(self, "_pending_timers", None)
        value = (self.coordinator.data or {}).get(TIMER_DPS)
        if pending and value and value != getattr(self, "_last_timer_value", None):
            self._last_timer_value = value
            parsed = parse_timer_response(value)
            if parsed and parsed[0] == TIMER_ADD and parsed[1] in pending:
                hour, minute, start = pending.pop(parsed[1])
                matches = [t for t in parsed[2] if t["single"] and t["hour"] == hour and t["minute"] == minute]
                if matches:
                    timer_id = max(t["id"] for t in matches)
                    delay = max((start - dt_util.now()).total_seconds(), 0) + 120
                    logger.info("One-shot timer %s created, deleting it in %ds", timer_id, delay)
                    async def _delete_later(_now, tid=timer_id) -> None:
                        await self._async_delete_timer(tid)

                    async_call_later(self.hass, delay, _delete_later)
        super()._handle_coordinator_update()

    async def _async_delete_timer(self, timer_id: int) -> None:
        self._timer_seq = (getattr(self, "_timer_seq", 400) + 1) % 60000 or 1
        payload = build_timer_delete(timer_id, self._timer_seq)
        logger.info("Deleting one-shot timer %s via DPS 164: %s", timer_id, payload)
        try:
            await self.coordinator.tuya_client.async_set({TIMER_DPS: payload})
        except Exception as err:  # noqa: BLE001
            logger.warning("Could not delete one-shot timer %s: %s", timer_id, err)

    async def async_get_segments(self):
        """Return the rooms the vacuum can clean (HA area cleaning)."""
        return [
            Segment(id=str(room_id), name=ROOM_NAMES.get(room_id, f"Raum {room_id}"))
            for room_id in sorted(ROOM_NAMES)
        ]

    async def async_clean_segments(self, segment_ids: list[str], **kwargs: Any) -> None:
        """Clean the rooms mapped to the requested HA areas."""
        await self.async_clean_rooms([int(segment_id) for segment_id in segment_ids])

    async def async_send_command(
        self, command: str, params: dict[str, Any] | list[Any] | None = None, **kwargs: Any
    ) -> None:
        """Support vacuum.send_command.

        room_clean: params {"rooms": [4, 2], "clean_times": 1, "clean_mode": "vacuum_mop"}
                    (clean_mode: vacuum | mop | vacuum_mop; default mirrors the robot)
                    or simply a list of room IDs [1, 3].
        """
        if command == "room_clean":
            if isinstance(params, list):
                await self.async_clean_rooms([int(r) for r in params])
                return
            params = params or {}
            rooms = params.get("rooms", [])
            if isinstance(rooms, (int, str)):
                rooms = [rooms]
            map_id = params.get("map_id")
            await self.async_clean_rooms(
                [int(r) for r in rooms],
                int(params.get("clean_times", 1)),
                int(map_id) if map_id is not None else None,
                params.get("clean_mode"),
            )
            return
        if command == "raw_dps" and isinstance(params, dict):
            logger.info("Sending raw DPS %s: %s", params.get("dps"), params.get("value"))
            await self.coordinator.tuya_client.async_set({str(params["dps"]): params["value"]})
            return
        if command == "raw_dps152":
            value = params.get("value") if isinstance(params, dict) else params
            logger.info("Sending raw DPS 152 value: %s", value)
            await self.coordinator.tuya_client.async_set({"152": value})
            return
        logger.warning("Unknown command for Eufy S1 Pro: %s", command)
