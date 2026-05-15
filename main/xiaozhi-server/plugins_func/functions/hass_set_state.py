from plugins_func.register import register_function, ToolType, ActionResponse, Action
from plugins_func.functions.hass_init import initialize_hass_handler
from config.logger import setup_logging
import asyncio
import re
import requests
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

hass_set_state_function_desc = {
    "type": "function",
    "function": {
        "name": "hass_set_state",
        "description": (
            "设置homeassistant里设备的状态。"
            "灯光：开/关、调亮度、调颜色、调色温。"
            "空调(climate)：开/关、设温度、设模式(制冷/制热/除湿/送风/自动)、设风速、设扫风。"
            "播放器：音量、暂停、继续、静音。"
            "晾衣架/窗帘(cover)：打开/关闭。"
            "选择器(select)：切换选项（如无风感、随身感、风向角度）。"
            "注意：设置无风感、扫风等功能前请确保空调已开机且处于制冷模式。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "state": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "description": (
                                "操作类型: "
                                "turn_on(打开), turn_off(关闭), "
                                "brightness_up(调亮), brightness_down(调暗), brightness_value(设置亮度), "
                                "set_color(设置颜色), set_kelvin(设置色温), "
                                "volume_up, volume_down, volume_set, volume_mute, "
                                "pause, continue, "
                                "set_temperature(设置温度), set_hvac_mode(设置模式), "
                                "set_fan_mode(设置风速), set_swing_mode(设置扫风), "
                                "set_preset_mode(设置预设), "
                                "set_option(设置选择器选项，如无风感)"
                            ),
                        },
                        "input": {
                            "type": "string",
                            "description": (
                                "操作参数值。亮度/音量时传1-100的数字；"
                                "温度时传数字(如26)；"
                                "模式/风速/扫风/预设/选择器选项时传对应字符串(如cool、low、off、up_no_wind)"
                            ),
                        },
                        "is_muted": {
                            "type": "string",
                            "description": "仅在静音操作时需要,true或false",
                        },
                        "rgb_color": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": "仅在设置颜色时需要,RGB数组",
                        },
                    },
                    "required": ["type"],
                },
                "entity_id": {
                    "type": "string",
                    "description": "设备的entity_id",
                },
            },
            "required": ["state", "entity_id"],
        },
    },
}


def _post_ha(base_url, api_key, domain, action, data):
    url = f"{base_url}/api/services/{domain}/{action}"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    return requests.post(url, headers=headers, json=data, timeout=10)


def _get_ha_states(base_url, api_key):
    url = f"{base_url}/api/states"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    resp = requests.get(url, headers=headers, timeout=10)
    return resp.json() if resp.status_code == 200 else []


def _find_related_climate(conn, base_url, api_key, entity_id):
    """根据 select/switch 的 entity_id 找同设备的气候实体"""
    device_id_match = re.search(r'[\._]([a-f0-9]{4,}|[\d]{10,})', entity_id)
    if not device_id_match:
        return None
    device_id = device_id_match.group(1)
    try:
        states = _get_ha_states(base_url, api_key)
        for s in states:
            if s["entity_id"].startswith("climate.") and device_id in s["entity_id"]:
                return s["entity_id"]
    except Exception:
        pass
    return None


def _ensure_cool_mode(base_url, api_key, entity_id):
    """确保空调处于制冷模式（无风感需要制冷模式）"""
    climate_entity = _find_related_climate(None, base_url, api_key, entity_id)
    if not climate_entity:
        return None
    # 先开机，再切制冷
    _post_ha(base_url, api_key, "climate", "turn_on", {"entity_id": climate_entity})
    resp = _post_ha(
        base_url, api_key, "climate", "set_hvac_mode",
        {"entity_id": climate_entity, "hvac_mode": "cool"},
    )
    logger.bind(tag=TAG).info(f"无风感前置: 设置{climate_entity}为制冷模式, code={resp.status_code}")
    return climate_entity


@register_function("hass_set_state", hass_set_state_function_desc, ToolType.SYSTEM_CTL)
def hass_set_state(conn: "ConnectionHandler", entity_id="", state=None):
    if state is None:
        state = {}
    try:
        ha_response = handle_hass_set_state(conn, entity_id, state)
        return ActionResponse(Action.REQLLM, ha_response, None)
    except asyncio.TimeoutError:
        logger.bind(tag=TAG).error("设置Home Assistant状态超时")
        return ActionResponse(Action.ERROR, "请求超时", None)
    except Exception as e:
        logger.bind(tag=TAG).error(f"执行Home Assistant操作失败: {e}")
        return ActionResponse(Action.ERROR, f"执行Home Assistant操作失败: {e}", None)


def handle_hass_set_state(conn: "ConnectionHandler", entity_id, state):
    ha_config = initialize_hass_handler(conn)
    api_key = ha_config.get("api_key")
    base_url = ha_config.get("base_url")

    domains = entity_id.split(".")
    if len(domains) > 1:
        domain = domains[0]
    else:
        return "执行失败，错误的设备id"

    action = ""
    arg = ""
    value = None
    description = ""
    state_type = state.get("type", "")
    state_input = state.get("input")

    # --- 通用开关 ---
    if state_type == "turn_on":
        description = "设备已打开"
        if domain == "cover":
            action, arg, value = "open_cover", "", None
        elif domain == "vacuum":
            action, arg, value = "start", "", None
        else:
            action, arg, value = "turn_on", "", None

    elif state_type == "turn_off":
        description = "设备已关闭"
        if domain == "cover":
            action, arg, value = "close_cover", "", None
        elif domain == "vacuum":
            action, arg, value = "stop", "", None
        else:
            action, arg, value = "turn_off", "", None

    # --- 灯光 ---
    elif state_type == "brightness_up":
        description, action, arg, value = "灯光已调亮", "turn_on", "brightness_step_pct", 10
    elif state_type == "brightness_down":
        description, action, arg, value = "灯光已调暗", "turn_on", "brightness_step_pct", -10
    elif state_type == "brightness_value":
        description = f"亮度已调整到{state_input}"
        action, arg, value = "turn_on", "brightness_pct", state_input
    elif state_type == "set_color":
        description = f"颜色已调整"
        action, arg, value = "turn_on", "rgb_color", state.get("rgb_color")
    elif state_type == "set_kelvin":
        description = f"色温已调整"
        action, arg, value = "turn_on", "kelvin", state_input

    # --- 播放器 ---
    elif state_type == "volume_up":
        description, action, arg, value = "音量已调大", state_type, "", None
    elif state_type == "volume_down":
        description, action, arg, value = "音量已调小", state_type, "", None
    elif state_type == "volume_set":
        description = f"音量已调整"
        val = float(state_input) if state_input else 0
        if val >= 1:
            val = val / 100
        action, arg, value = state_type, "volume_level", val
    elif state_type == "volume_mute":
        description, action, arg, value = "设备已静音", state_type, "is_volume_muted", state.get("is_muted")
    elif state_type == "pause":
        description = "设备已暂停"
        if domain == "media_player":
            action = "media_pause"
        elif domain == "cover":
            action = "stop_cover"
        elif domain == "vacuum":
            action = "pause"
        arg, value = "", None
    elif state_type == "continue":
        description = "设备已继续"
        if domain == "media_player":
            action = "media_play"
        elif domain == "vacuum":
            action = "start"
        arg, value = "", None

    # --- 空调：温度 ---
    elif state_type == "set_temperature":
        if domain != "climate":
            return "set_temperature 仅支持 climate 类型设备"
        description = f"温度已设置为{state_input}°C"
        action, arg, value = "set_temperature", "temperature", float(state_input)

    # --- 空调：模式 ---
    elif state_type == "set_hvac_mode":
        if domain != "climate":
            return "set_hvac_mode 仅支持 climate 类型设备"
        mode_map = {
            "制冷": "cool", "制热": "heat", "除湿": "dry",
            "送风": "fan_only", "自动": "auto", "关": "off",
        }
        hvac_mode = mode_map.get(state_input, state_input)
        description = f"模式已设置为{hvac_mode}"
        action, arg, value = "set_hvac_mode", "hvac_mode", hvac_mode

    # --- 空调：风速 ---
    elif state_type == "set_fan_mode":
        if domain != "climate":
            return "set_fan_mode 仅支持 climate 类型设备"
        fan_map = {
            "静音": "silent", "低": "low", "中": "medium",
            "高": "high", "强劲": "full", "自动": "auto",
        }
        fan_mode = fan_map.get(state_input, state_input)
        description = f"风速已设置为{fan_mode}"
        action, arg, value = "set_fan_mode", "fan_mode", fan_mode

    # --- 空调：扫风 ---
    elif state_type == "set_swing_mode":
        if domain != "climate":
            return "set_swing_mode 仅支持 climate 类型设备"
        swing_map = {
            "关": "off", "上下": "vertical", "左右": "horizontal",
            "同时": "both", "关闭": "off",
        }
        swing_mode = swing_map.get(state_input, state_input)
        description = f"扫风已设置为{swing_mode}"
        action, arg, value = "set_swing_mode", "swing_mode", swing_mode

    # --- 空调：预设 ---
    elif state_type == "set_preset_mode":
        if domain != "climate":
            return "set_preset_mode 仅支持 climate 类型设备"
        preset_map = {
            "节能": "eco", "舒适": "comfort", "强力": "boost",
            "无": "none", "关闭": "none",
        }
        preset_mode = preset_map.get(state_input, state_input)
        description = f"预设已设置为{preset_mode}"
        action, arg, value = "set_preset_mode", "preset_mode", preset_mode

    # --- 选择器（无风感、方向等） ---
    elif state_type == "set_option":
        if domain != "select":
            return "set_option 仅支持 select 类型设备"
        option = str(state_input)
        # 无风感：需要先切换制冷模式
        if option != "off" and ("no_wind" in entity_id or "wind" in entity_id.lower()):
            _ensure_cool_mode(base_url, api_key, entity_id)
        description = f"已设置为{option}"
        action, arg, value = "select_option", "option", option

    else:
        return f"{domain} {state_type}功能尚未支持"

    # 发送请求
    if arg:
        data = {"entity_id": entity_id, arg: value}
    else:
        data = {"entity_id": entity_id}

    response = _post_ha(base_url, api_key, domain, action, data)
    logger.bind(tag=TAG).info(
        f"设置状态:{description}, url:/api/services/{domain}/{action}, return_code:{response.status_code}"
    )
    if response.status_code == 200:
        return description
    else:
        return f"设置失败，错误码: {response.status_code}"
