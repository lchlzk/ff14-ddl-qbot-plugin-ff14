from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from bot_tools.request_cache import ResponseCache, singleflight
from nonebot import get_driver, on_command
from nonebot.adapters import Event, Message
from nonebot.log import logger
from nonebot.matcher import Matcher
from nonebot.params import CommandArg

from message_ui import (
    DIVIDER, cooldown_panel, error_panel, help_panel, panel,
    public_error_message,
)


GLOBAL_HOST = "https://www.fflogs.com"
CN_HOST = "https://cn.fflogs.com"
DEFAULT_TIMEOUT_SECONDS = 18.0
DEFAULT_CACHE_TTL_SECONDS = 600.0
DEFAULT_GLOBAL_INTERVAL_SECONDS = 0.5
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_REPLY_BYTES = 1800
MAX_ARGUMENT_CHARACTERS = 240


def _number_from_environment(
    name: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    raw_value = os.environ.get(name, "").strip()
    if not raw_value:
        return default
    try:
        value = float(raw_value)
    except ValueError:
        logger.warning("Ignoring invalid {} value", name)
        return default
    return min(max(value, minimum), maximum)


def _optional_zone_from_environment() -> int | None:
    raw_value = os.environ.get("FFLOGS_DEFAULT_ZONE_ID", "").strip()
    if not raw_value:
        return None
    if raw_value.isascii() and raw_value.isdigit():
        value = int(raw_value)
        if 1 <= value <= 100_000:
            return value
    logger.warning("Ignoring invalid FFLOGS_DEFAULT_ZONE_ID value")
    return None


FFLOGS_CLIENT_ID = os.environ.get("FFLOGS_CLIENT_ID", "").strip()
FFLOGS_CLIENT_SECRET = os.environ.get("FFLOGS_CLIENT_SECRET", "").strip()
FFLOGS_DEFAULT_REGION = os.environ.get("FFLOGS_DEFAULT_REGION", "CN").strip().upper()
FFLOGS_DEFAULT_ZONE_ID = _optional_zone_from_environment()
FFLOGS_TIMEOUT = _number_from_environment(
    "FFLOGS_TIMEOUT", DEFAULT_TIMEOUT_SECONDS, 3.0, 30.0
)
FFLOGS_CACHE_TTL = _number_from_environment(
    "FFLOGS_CACHE_TTL", DEFAULT_CACHE_TTL_SECONDS, 0.0, 3600.0
)
FFLOGS_GLOBAL_INTERVAL = _number_from_environment(
    "FFLOGS_GLOBAL_INTERVAL", DEFAULT_GLOBAL_INTERVAL_SECONDS, 0.0, 10.0
)


class FFLogsError(RuntimeError):
    """An expected FF Logs error that is safe to display in QQ."""


@dataclass(frozen=True)
class CharacterQuery:
    name: str
    server: str
    region: str
    zone_id: int | None = None
    job: str | None = None
    metric: str = "rdps"
    floor: str | None = None


_REGION_ALIASES = {
    "国服": "CN",
    "中国": "CN",
    "CN": "CN",
    "日服": "JP",
    "JP": "JP",
    "美服": "NA",
    "NA": "NA",
    "US": "NA",
    "欧服": "EU",
    "EU": "EU",
    "韩服": "KR",
    "KR": "KR",
    "澳服": "OC",
    "OC": "OC",
    "OCE": "OC",
}
_VALID_REGIONS = frozenset(_REGION_ALIASES.values())

_JOB_ALIASES = {
    "paladin": "Paladin",
    "pld": "Paladin",
    "骑士": "Paladin",
    "warrior": "Warrior",
    "war": "Warrior",
    "战士": "Warrior",
    "darkknight": "DarkKnight",
    "drk": "DarkKnight",
    "暗骑": "DarkKnight",
    "暗黑骑士": "DarkKnight",
    "gunbreaker": "Gunbreaker",
    "gnb": "Gunbreaker",
    "绝枪": "Gunbreaker",
    "绝枪战士": "Gunbreaker",
    "whitemage": "WhiteMage",
    "whm": "WhiteMage",
    "白魔": "WhiteMage",
    "白魔法师": "WhiteMage",
    "scholar": "Scholar",
    "sch": "Scholar",
    "学者": "Scholar",
    "astrologian": "Astrologian",
    "ast": "Astrologian",
    "占星": "Astrologian",
    "占星术士": "Astrologian",
    "sage": "Sage",
    "sge": "Sage",
    "贤者": "Sage",
    "monk": "Monk",
    "mnk": "Monk",
    "武僧": "Monk",
    "dragoon": "Dragoon",
    "drg": "Dragoon",
    "龙骑": "Dragoon",
    "龙骑士": "Dragoon",
    "ninja": "Ninja",
    "nin": "Ninja",
    "忍者": "Ninja",
    "samurai": "Samurai",
    "sam": "Samurai",
    "武士": "Samurai",
    "reaper": "Reaper",
    "rpr": "Reaper",
    "钐镰": "Reaper",
    "钐镰客": "Reaper",
    "镰刀": "Reaper",
    "镰": "Reaper",
    "viper": "Viper",
    "vpr": "Viper",
    "蝰蛇": "Viper",
    "蝰蛇剑士": "Viper",
    "bard": "Bard",
    "brd": "Bard",
    "诗人": "Bard",
    "吟游诗人": "Bard",
    "machinist": "Machinist",
    "mch": "Machinist",
    "机工": "Machinist",
    "机工士": "Machinist",
    "dancer": "Dancer",
    "dnc": "Dancer",
    "舞者": "Dancer",
    "blackmage": "BlackMage",
    "blm": "BlackMage",
    "黑魔": "BlackMage",
    "黑魔法师": "BlackMage",
    "summoner": "Summoner",
    "smn": "Summoner",
    "召唤": "Summoner",
    "召唤师": "Summoner",
    "redmage": "RedMage",
    "rdm": "RedMage",
    "赤魔": "RedMage",
    "赤魔法师": "RedMage",
    "pictomancer": "Pictomancer",
    "pct": "Pictomancer",
    "绘灵": "Pictomancer",
    "绘灵法师": "Pictomancer",
    "画家": "Pictomancer",
    "bluemage": "BlueMage",
    "blu": "BlueMage",
    "青魔": "BlueMage",
    "青魔法师": "BlueMage",
    "beastmaster": "Beastmaster",
    "bst": "Beastmaster",
    "魔兽": "Beastmaster",
    "魔兽使": "Beastmaster",
}

_JOB_DISPLAY = {
    "Paladin": "骑士",
    "Warrior": "战士",
    "DarkKnight": "暗黑骑士",
    "Gunbreaker": "绝枪战士",
    "WhiteMage": "白魔法师",
    "Scholar": "学者",
    "Astrologian": "占星术士",
    "Sage": "贤者",
    "Monk": "武僧",
    "Dragoon": "龙骑士",
    "Ninja": "忍者",
    "Samurai": "武士",
    "Reaper": "钐镰客",
    "Viper": "蝰蛇剑士",
    "Bard": "吟游诗人",
    "Machinist": "机工士",
    "Dancer": "舞者",
    "BlackMage": "黑魔法师",
    "Summoner": "召唤师",
    "RedMage": "赤魔法师",
    "Pictomancer": "绘灵法师",
    "BlueMage": "青魔法师",
    "Beastmaster": "魔兽使",
}

_METRIC_NAMES = {
    "rdps": "rDPS",
    "ndps": "nDPS",
    "cdps": "cDPS",
    "dps": "DPS",
}

# FF Logs exposes zone and encounter IDs, while raiders commonly use floor
# shorthands such as M9S. Keep that presentation layer explicit: unlike the API
# IDs these aliases are community names and are not returned by GraphQL.
_ZONE_SHORT_NAMES = {
    73: "M9S–M12S",
    68: "M5S–M8S",
    62: "M1S–M4S",
    54: "P9S–P12S",
    49: "P5S–P8S",
    44: "P1S–P4S",
    38: "E9S–E12S",
    33: "E5S–E8S",
    29: "E1S–E4S",
    25: "O9S–O12S",
    21: "O5S–O8S",
    17: "O1S–O4S",
    13: "A9S–A12S",
    10: "A5S–A8S",
    7: "A1S–A4S",
}


def _floor_aliases(prefix: str, start: int, encounter_ids: tuple[int, ...]) -> dict[int, str]:
    aliases: dict[int, str] = {}
    for index, encounter_id in enumerate(encounter_ids):
        if index < 3:
            aliases[encounter_id] = f"{prefix}{start + index}S"
        elif len(encounter_ids) > 4:
            aliases[encounter_id] = f"{prefix}{start + 3}S P{index - 2}"
        else:
            aliases[encounter_id] = f"{prefix}{start + 3}S"
    return aliases


_ZONE_ENCOUNTER_ALIASES = {
    73: _floor_aliases("M", 9, (101, 102, 103, 104, 105)),
    68: _floor_aliases("M", 5, (97, 98, 99, 100)),
    62: _floor_aliases("M", 1, (93, 94, 95, 96)),
    54: _floor_aliases("P", 9, (88, 89, 90, 91, 92)),
    49: _floor_aliases("P", 5, (83, 84, 85, 86, 87)),
    44: _floor_aliases("P", 1, (78, 79, 80, 81, 82)),
    38: _floor_aliases("E", 9, (73, 74, 75, 76, 77)),
    33: _floor_aliases("E", 5, (69, 70, 71, 72)),
    29: _floor_aliases("E", 1, (65, 66, 67, 68)),
    25: _floor_aliases("O", 9, (60, 61, 62, 63, 64)),
    21: _floor_aliases("O", 5, (51, 52, 53, 54, 55)),
    17: _floor_aliases("O", 1, (42, 43, 44, 45, 46)),
    13: _floor_aliases("A", 9, (34, 35, 36, 37)),
    10: _floor_aliases("A", 5, (26, 27, 28, 29)),
    7: _floor_aliases("A", 1, (18, 19, 20, 21)),
}

_ZONE_BY_FLOOR_ALIAS: dict[str, int] = {}
_CANONICAL_FLOOR_ALIASES: dict[str, str] = {}
for _alias_zone_id, _encounter_aliases in _ZONE_ENCOUNTER_ALIASES.items():
    for _floor_name in _encounter_aliases.values():
        _base_floor_name = _floor_name.split()[0]
        _normalized_floor_name = _base_floor_name.casefold()
        _ZONE_BY_FLOOR_ALIAS[_normalized_floor_name] = _alias_zone_id
        _CANONICAL_FLOOR_ALIASES[_normalized_floor_name] = _base_floor_name

_CONTROL_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_request_slots = asyncio.Semaphore(3)
_token_lock = asyncio.Lock()
_global_rate_lock = asyncio.Lock()
_cache_lock = asyncio.Lock()
_zone_cache_lock = asyncio.Lock()
_cooldown_lock = asyncio.Lock()
_http_client: httpx.AsyncClient | None = None
_access_tokens: dict[str, tuple[str, float]] = {}
_last_api_request_at = 0.0
_response_cache = ResponseCache()
_zone_catalog_cache: dict[str, tuple[float, tuple[Mapping[str, Any], ...]]] = {}
_zone_names: dict[tuple[str, int], str] = {}
_last_command_by_user: dict[str, float] = {}

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


CHARACTER_RANKINGS_QUERY = """
query CharacterRankings(
  $name: String!
  $serverSlug: String!
  $serverRegion: String!
  $zoneID: Int
  $specName: String
  $metric: CharacterPageRankingMetricType
) {
  characterData {
    character(
      name: $name
      serverSlug: $serverSlug
      serverRegion: $serverRegion
    ) {
      id
      name
      hidden
      server {
        name
        slug
        region {
          compactName
          name
        }
      }
      zoneRankings(
        zoneID: $zoneID
        specName: $specName
        metric: $metric
        compare: Rankings
        timeframe: Historical
        byBracket: false
        includePrivateLogs: false
      )
    }
  }
  rateLimitData {
    limitPerHour
    pointsSpentThisHour
    pointsResetIn
  }
}
""".strip()

WORLD_ZONES_QUERY = """
query WorldZones {
  worldData {
    zones {
      id
      name
      frozen
      encounters {
        id
        name
      }
    }
  }
}
""".strip()

ZONE_CACHE_TTL_SECONDS = 6 * 60 * 60


def configuration_error() -> str | None:
    if not FFLOGS_CLIENT_ID and not FFLOGS_CLIENT_SECRET:
        return (
            "尚未配置 FF Logs API。请先运行 bot.cmd fflogs-setup"
            "（Linux：./bot.sh fflogs-setup）。"
        )
    if not FFLOGS_CLIENT_ID or not FFLOGS_CLIENT_SECRET:
        return "FF Logs Client ID/Secret 配置不完整，请重新运行 fflogs-setup。"
    for value, label, maximum in (
        (FFLOGS_CLIENT_ID, "Client ID", 256),
        (FFLOGS_CLIENT_SECRET, "Client Secret", 512),
    ):
        if len(value) > maximum or any(ord(character) < 32 for character in value):
            return f"FF Logs {label} 格式不正确。"
    if FFLOGS_DEFAULT_REGION not in _VALID_REGIONS:
        return "FFLOGS_DEFAULT_REGION 必须是 CN、JP、NA、EU、KR 或 OC。"
    return None


def public_configuration_error() -> str | None:
    if configuration_error():
        return "FF Logs 查询暂时不可用，请稍后重试或联系管理员。"
    return None


def _normalize_region(value: str) -> str | None:
    normalized = value.strip().upper()
    return _REGION_ALIASES.get(normalized)


def _normalize_job(value: str) -> str | None:
    normalized = re.sub(r"[\s_-]+", "", value.strip()).lower()
    return _JOB_ALIASES.get(normalized)


def _normalize_metric(value: str) -> str | None:
    return value.strip().lower() if value.strip().lower() in _METRIC_NAMES else None


def _host_for_region(region: str) -> str:
    return CN_HOST if region.upper() == "CN" else GLOBAL_HOST


def _zone_reference(value: str) -> int | None:
    normalized = value.strip().casefold().replace("–", "-").replace("—", "-")
    if normalized.isascii() and normalized.isdigit():
        parsed = int(normalized)
        return parsed if 1 <= parsed <= 100_000 else None
    if normalized in _ZONE_BY_FLOOR_ALIAS:
        return _ZONE_BY_FLOOR_ALIAS[normalized]
    for zone_id, short_name in _ZONE_SHORT_NAMES.items():
        comparable = short_name.casefold().replace("–", "-").replace("—", "-")
        if normalized == comparable:
            return zone_id
    return None


def _floor_reference(value: str) -> str | None:
    return _CANONICAL_FLOOR_ALIASES.get(value.strip().casefold())


def _character_command_help(command: str) -> str:
    title = "伤害排名查询" if command == "dps" else "团本战绩查询"
    return help_panel(
        title,
        [
            f"/{command} 角色名 服务器",
            f"/{command} 角色名 服务器 职业=武士 副本=73",
            "",
            "可选参数",
            "• 区域=国服 / JP / NA / EU / KR / OC",
            "• 副本=73 或 副本=M9S　职业=职业名",
            "• 指标=rdps / ndps / cdps / dps",
            "",
            "英文角色名可用竖线分隔：",
            f"/{command} North Face | Chocobo | JP | 副本=76",
        ],
        footer="发送 /fflogs zones 查看副本编号。",
    )


def _parse_modifier(
    value: str,
    region: str,
    zone_id: int | None,
    job: str | None,
    metric: str,
) -> tuple[str, int | None, str | None, str, str | None]:
    raw_value = value.strip()
    if "=" in raw_value:
        raw_key, raw_option = raw_value.split("=", 1)
    else:
        raw_key, raw_option = "", ""
    key = raw_key.strip().lower()
    if key in {"region", "区域", "区服"}:
        parsed_region = _normalize_region(raw_option)
        if not parsed_region:
            return region, zone_id, job, metric, "区域格式不正确，例如 区域=国服。"
        return parsed_region, zone_id, job, metric, None
    if key in {"zone", "副本"}:
        zone_text = raw_option.strip()
        parsed_zone = _zone_reference(zone_text)
        if parsed_zone is None:
            return region, zone_id, job, metric, (
                "副本可填写正整数 ID 或零式简称，例如 副本=73、"
                "副本=M9S；"
                "发送 /fflogs zones 可以查看编号。"
            )
        return region, parsed_zone, job, metric, None
    if key in {"job", "职业"}:
        parsed_job = _normalize_job(raw_option)
        if not parsed_job:
            return region, zone_id, job, metric, "无法识别这个职业，可使用中文、英文或职业缩写。"
        return region, zone_id, parsed_job, metric, None
    if key in {"metric", "指标"}:
        parsed_metric = _normalize_metric(raw_option)
        if not parsed_metric:
            return region, zone_id, job, metric, "指标可使用 rdps、ndps、cdps 或 dps。"
        return region, zone_id, job, parsed_metric, None

    parsed_region = _normalize_region(raw_value)
    if parsed_region and raw_value.upper() in _REGION_ALIASES:
        return parsed_region, zone_id, job, metric, None
    if raw_value.isascii() and raw_value.isdigit():
        parsed_zone = int(raw_value)
        if 1 <= parsed_zone <= 100_000:
            return region, parsed_zone, job, metric, None
        return region, zone_id, job, metric, "副本 ID 超出允许范围。"
    parsed_zone = _zone_reference(raw_value)
    if parsed_zone is not None:
        return region, parsed_zone, job, metric, None
    parsed_metric = _normalize_metric(raw_value)
    if parsed_metric:
        return region, zone_id, job, parsed_metric, None
    parsed_job = _normalize_job(raw_value)
    if parsed_job:
        return region, zone_id, parsed_job, metric, None
    return region, zone_id, job, metric, f"无法识别参数“{_safe_piece(raw_value, 40)}”。"


def parse_character_arguments(
    raw_arguments: str,
    command: str,
) -> tuple[CharacterQuery | None, str | None]:
    usage = _character_command_help(command)
    raw_arguments = raw_arguments.strip()
    if not raw_arguments or raw_arguments.lower() == "help":
        return None, usage
    if len(raw_arguments) > MAX_ARGUMENT_CHARACTERS:
        return None, f"参数过长，请控制在 {MAX_ARGUMENT_CHARACTERS} 个字符以内。"

    modifiers: list[str]
    if "|" in raw_arguments:
        parts = [part.strip() for part in raw_arguments.split("|")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            return None, usage
        name, server = parts[0], parts[1]
        modifiers = [part for part in parts[2:] if part]
    else:
        tokens = raw_arguments.split()
        if command == "dps" and any(
            (token.isascii() and token.isdigit())
            or re.fullmatch(r"day#\d+", token, flags=re.IGNORECASE)
            for token in tokens
        ):
            return None, (
                "旧版“Boss 职业 数值”查询依赖的网页数据源已停用。\n"
                "新版请使用：/dps <角色名> <服务器> [job=职业] "
                "[metric=rdps] [zone=副本ID]"
            )
        modifiers = [
            token
            for token in tokens
            if re.match(
                r"^(?:region|区域|区服|zone|副本|job|职业|metric|指标)=",
                token,
                flags=re.IGNORECASE,
            )
            or re.fullmatch(r"[ameop][0-9]{1,2}s", token, flags=re.IGNORECASE)
        ]
        positional = [token for token in tokens if token not in modifiers]
        if len(positional) < 2:
            return None, usage
        name = " ".join(positional[:-1])
        server = positional[-1]

    if not 1 <= len(name) <= 64 or not 1 <= len(server) <= 40:
        return None, "角色名或服务器名过长。"
    if any(character in name + server for character in "\r\n\x00"):
        return None, "角色名或服务器名包含无效字符。"

    region = FFLOGS_DEFAULT_REGION
    zone_id = FFLOGS_DEFAULT_ZONE_ID
    job: str | None = None
    metric = "rdps"
    floor: str | None = None
    for modifier in modifiers:
        modifier_value = modifier.split("=", 1)[1] if "=" in modifier else modifier
        requested_floor = _floor_reference(modifier_value)
        if requested_floor:
            floor = requested_floor
        region, zone_id, job, metric, error = _parse_modifier(
            modifier, region, zone_id, job, metric
        )
        if error:
            return None, error
    return CharacterQuery(name, server, region, zone_id, job, metric, floor), None


def _safe_piece(value: Any, maximum: int = 100) -> str:
    text = _CONTROL_PATTERN.sub("", str(value or ""))
    text = text.replace("\r", " ").replace("\n", " ").strip()
    return text[:maximum]


def _clean_reply(value: Any) -> str:
    text = _CONTROL_PATTERN.sub("", str(value or ""))
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_REPLY_BYTES:
        return text
    suffix = "\n……内容过长"
    candidate = text
    while candidate and len((candidate + suffix).encode("utf-8")) > MAX_REPLY_BYTES:
        candidate = candidate[:-1]
    return candidate.rstrip() + suffix


def _finite_number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _format_number(value: Any, decimals: int = 1) -> str | None:
    number = _finite_number(value)
    if number is None:
        return None
    return f"{number:,.{decimals}f}"


def _format_positive_number(value: Any, decimals: int = 1) -> str | None:
    number = _finite_number(value)
    if number is None or number <= 0:
        return None
    return f"{number:,.{decimals}f}"


def _format_rank_percent(value: Any) -> str | None:
    """Match the integer percentile shown on FF Logs character pages."""
    number = _finite_number(value)
    if number is None or number <= 0:
        return None
    return str(math.floor(min(number, 100.0)))


def _format_duration_ms(value: Any) -> str | None:
    milliseconds = _finite_number(value)
    if milliseconds is None or milliseconds <= 0:
        return None
    # FF Logs drops fractional seconds in its character-page table.
    seconds = int(milliseconds // 1000.0)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def _display_job(value: Any) -> str:
    raw_value = _safe_piece(value, 40)
    return _JOB_DISPLAY.get(raw_value, raw_value)


def _rankings_payload(character: Mapping[str, Any]) -> Mapping[str, Any]:
    if bool(character.get("hidden")):
        raise FFLogsError("该角色已隐藏 FF Logs 排名。")
    payload = character.get("zoneRankings")
    if not isinstance(payload, Mapping):
        raise FFLogsError("FF Logs 没有返回可用的排名数据。")
    if payload.get("error"):
        error_text = str(payload.get("error", "")).lower()
        if "permission" in error_text or "hidden" in error_text:
            raise FFLogsError("该角色已隐藏 FF Logs 排名。")
        raise FFLogsError("FF Logs 暂时无法计算该角色的排名。")
    return payload


def _encounter_entries(
    payload: Mapping[str, Any], query: CharacterQuery | None = None
) -> list[Mapping[str, Any]]:
    candidates = payload.get("rankings")
    if not isinstance(candidates, list):
        candidates = payload.get("encounterRanks")
    if not isinstance(candidates, list):
        return []
    entries = [item for item in candidates if isinstance(item, Mapping)][:20]
    if query is None or not query.floor:
        return entries
    zone_id = _zone_identifier(payload, query)
    if zone_id is None:
        return entries
    aliases = _ZONE_ENCOUNTER_ALIASES.get(zone_id, {})
    filtered: list[Mapping[str, Any]] = []
    for item in entries:
        encounter = item.get("encounter")
        raw_identifier = encounter.get("id") if isinstance(encounter, Mapping) else None
        if raw_identifier is None:
            raw_identifier = item.get("encounterID") or item.get("encounter_id")
        encounter_number = _finite_number(raw_identifier)
        alias = aliases.get(int(encounter_number)) if encounter_number is not None else None
        if alias and alias.split()[0].casefold() == query.floor.casefold():
            filtered.append(item)
    return filtered


def _encounter_name(item: Mapping[str, Any]) -> str:
    encounter = item.get("encounter")
    if isinstance(encounter, Mapping):
        return _safe_piece(encounter.get("name") or encounter.get("id"), 80)
    return (
        _safe_piece(encounter, 80)
        or _safe_piece(item.get("encounterID") or item.get("encounter_id"), 80)
        or "未知 Boss"
    )


def _zone_identifier(payload: Mapping[str, Any], query: CharacterQuery) -> int | None:
    zone = payload.get("zone")
    raw_identifier = zone.get("id") if isinstance(zone, Mapping) else zone
    number = _finite_number(raw_identifier)
    if number is not None:
        return int(number)
    return query.zone_id


def _encounter_label(
    item: Mapping[str, Any], payload: Mapping[str, Any], query: CharacterQuery
) -> str:
    name = _encounter_name(item)
    encounter = item.get("encounter")
    raw_identifier = encounter.get("id") if isinstance(encounter, Mapping) else None
    if raw_identifier is None:
        raw_identifier = item.get("encounterID") or item.get("encounter_id")
    encounter_number = _finite_number(raw_identifier)
    zone_id = _zone_identifier(payload, query)
    if encounter_number is None or zone_id is None:
        return name
    alias = _ZONE_ENCOUNTER_ALIASES.get(zone_id, {}).get(int(encounter_number))
    return f"{alias} · {name}" if alias else name


def _total_kills(item: Mapping[str, Any]) -> int:
    return int(
        _finite_number(item.get("totalKills"))
        or _finite_number(item.get("total_kills"))
        or _finite_number(item.get("kills"))
        or 0
    )


def _has_public_record(item: Mapping[str, Any]) -> bool:
    return _total_kills(item) > 0 or any(
        (_finite_number(item.get(key)) or 0) > 0
        for key in ("bestAmount", "best_amount", "rankPercent", "rank_percent")
    )


def _character_heading(character: Mapping[str, Any], query: CharacterQuery) -> str:
    character_name = _safe_piece(character.get("name") or query.name, 64)
    server_data = character.get("server")
    if isinstance(server_data, Mapping):
        server_name = _safe_piece(server_data.get("name") or query.server, 40)
        region_data = server_data.get("region")
        if isinstance(region_data, Mapping):
            region_name = _safe_piece(
                region_data.get("compactName") or query.region, 12
            )
        else:
            region_name = query.region
    else:
        server_name = query.server
        region_name = query.region
    return f"{character_name}｜{server_name}｜{region_name}"


def _zone_label(payload: Mapping[str, Any], query: CharacterQuery) -> str:
    zone = payload.get("zone")
    identifier: int | None = None
    if isinstance(zone, Mapping):
        name = _safe_piece(zone.get("name"), 80)
        raw_identifier = _finite_number(zone.get("id"))
        identifier = int(raw_identifier) if raw_identifier is not None else None
        if name:
            if identifier is None:
                return name
            short_name = query.floor or _ZONE_SHORT_NAMES.get(identifier)
            detail = f"{short_name} · ID {identifier}" if short_name else f"ID {identifier}"
            return f"{name}（{detail}）"
        if identifier is not None:
            cached_name = _zone_names.get((_host_for_region(query.region), identifier))
            short_name = query.floor or _ZONE_SHORT_NAMES.get(identifier)
            detail = f"{short_name} · ID {identifier}" if short_name else f"ID {identifier}"
            return f"{cached_name}（{detail}）" if cached_name else f"副本 {detail}"
    elif zone is not None and str(zone).strip():
        raw_identifier = _finite_number(zone)
        identifier = int(raw_identifier) if raw_identifier is not None else None
    if identifier is None:
        identifier = query.zone_id
    if identifier is not None:
        cached_name = _zone_names.get((_host_for_region(query.region), identifier))
        short_name = query.floor or _ZONE_SHORT_NAMES.get(identifier)
        detail = f"{short_name} · ID {identifier}" if short_name else f"ID {identifier}"
        return f"{cached_name}（{detail}）" if cached_name else f"副本 {detail}"
    return "FF Logs 当前副本"


def _all_stars_lines(payload: Mapping[str, Any], limit: int = 3) -> list[str]:
    all_stars = payload.get("allStars")
    if not isinstance(all_stars, list):
        return []
    records = [item for item in all_stars if isinstance(item, Mapping)]
    records.sort(
        key=lambda item: _finite_number(item.get("rankPercent")) or -1.0,
        reverse=True,
    )
    lines: list[str] = []
    for item in records[:limit]:
        job = _display_job(item.get("spec"))
        percent = _format_rank_percent(item.get("rankPercent"))
        points = _format_positive_number(item.get("points"))
        rank = item.get("rank")
        pieces = [job] if job else []
        if percent:
            pieces.append(f"{percent}%")
        if points:
            pieces.append(f"All-Star {points}")
        if rank is not None:
            pieces.append(f"排名 #{_safe_piece(rank, 20)}")
        if pieces:
            lines.append(" · ".join(pieces))
    return lines


def format_raid_result(character: Mapping[str, Any], query: CharacterQuery) -> str:
    payload = _rankings_payload(character)
    entries = _encounter_entries(payload, query)
    heading = _character_heading(character, query)
    lines = [f"👤 {heading}", f"🗺️ {_zone_label(payload, query)}"]
    if not entries:
        lines.append("📭 暂无符合排名规则的公开记录")
    else:
        cleared = sum(1 for item in entries if _has_public_record(item))
        lines.extend((DIVIDER, f"公开记录　{cleared}/{len(entries)} 个 Boss"))
        for item in entries[:8]:
            name = _encounter_label(item, payload, query)
            kills = _total_kills(item)
            percent = _format_rank_percent(item.get("rankPercent"))
            fastest = _format_duration_ms(item.get("fastestKill"))
            if _has_public_record(item):
                details = [f"公开击杀 {kills} 次"] if kills > 0 else ["有公开记录"]
                if percent:
                    details.append(f"历史最佳 {percent}%")
                if fastest:
                    details.append(f"最快 {fastest}")
                lines.extend((f"● {name}", f"  {' · '.join(details)}"))
            else:
                lines.append(f"○ {name}　暂无公开排名")
    star_lines = _all_stars_lines(payload)
    if star_lines:
        lines.extend((DIVIDER, "⭐ All-Star", *[f"  {line}" for line in star_lines]))
    return _clean_reply(
        panel(
            "FF Logs · 团本历史战绩",
            lines,
            icon="🛡️",
            footer="百分位按战斗发生时的历史榜单计算；暂无排名不代表未通关。",
        )
    )


def format_dps_result(character: Mapping[str, Any], query: CharacterQuery) -> str:
    payload = _rankings_payload(character)
    entries = _encounter_entries(payload, query)
    heading = _character_heading(character, query)
    metric_name = _METRIC_NAMES.get(query.metric, query.metric)
    lines = [f"👤 {heading}", f"🗺️ {_zone_label(payload, query)}"]
    best_average = _format_positive_number(payload.get("bestPerformanceAverage"))
    median_average = _format_positive_number(payload.get("medianPerformanceAverage"))
    averages: list[str] = []
    if best_average:
        averages.append(f"历史最佳均值 {best_average}%")
    if median_average:
        averages.append(f"历史中位均值 {median_average}%")
    if averages:
        lines.append(" · ".join(averages))
    if not entries:
        lines.append(f"📭 暂无符合排名规则的公开 {metric_name} 记录")
    else:
        lines.append(DIVIDER)
        for item in entries[:8]:
            name = _encounter_label(item, payload, query)
            if not _has_public_record(item):
                lines.append(f"○ {name}　暂无公开排名")
                continue
            job = _display_job(item.get("bestSpec") or item.get("spec"))
            amount = _format_positive_number(item.get("bestAmount"))
            best_percent = _format_rank_percent(item.get("rankPercent"))
            median_percent = _format_rank_percent(item.get("medianPercent"))
            total_kills = _total_kills(item)
            fastest = _format_duration_ms(item.get("fastestKill"))
            performance = [job] if job else []
            if amount:
                performance.append(f"{amount} {metric_name}")
            if best_percent:
                performance.append(f"历史最佳 {best_percent}%")
            if median_percent:
                performance.append(f"历史中位 {median_percent}%")
            activity: list[str] = []
            if total_kills:
                activity.append(f"公开击杀 {total_kills}")
            if fastest:
                activity.append(f"最快 {fastest}")
            lines.append(f"● {name}")
            if performance:
                lines.append(f"  {' · '.join(performance)}")
            if activity:
                lines.append(f"  {' · '.join(activity)}")
    return _clean_reply(
        panel(
            f"FF Logs · {metric_name} 历史排名",
            lines,
            icon="📊",
            footer="百分位按战斗发生时的历史榜单计算；只统计公开上传且合规的实绩。",
        )
    )


async def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=min(5.0, FFLOGS_TIMEOUT),
                read=FFLOGS_TIMEOUT,
                write=min(10.0, FFLOGS_TIMEOUT),
                pool=min(5.0, FFLOGS_TIMEOUT),
            ),
            limits=httpx.Limits(max_connections=6, max_keepalive_connections=3),
            headers={"Accept": "application/json", "User-Agent": "NoneBot-FFLogs/1.0"},
            follow_redirects=False,
        )
    return _http_client


async def _decode_json_response(response: httpx.Response) -> Mapping[str, Any]:
    declared_length = response.headers.get("content-length")
    if declared_length and declared_length.isdigit():
        if int(declared_length) > MAX_RESPONSE_BYTES:
            raise FFLogsError("FF Logs 返回内容过大，已停止处理。")
    chunks: list[bytes] = []
    received = 0
    async for chunk in response.aiter_bytes():
        received += len(chunk)
        if received > MAX_RESPONSE_BYTES:
            raise FFLogsError("FF Logs 返回内容过大，已停止处理。")
        chunks.append(chunk)
    try:
        payload = json.loads(b"".join(chunks).decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FFLogsError("FF Logs 返回了无法解析的数据。") from exc
    if not isinstance(payload, Mapping):
        raise FFLogsError("FF Logs 返回格式不正确。")
    return payload


async def _get_access_token(host: str, force_refresh: bool = False) -> str:
    now = time.monotonic()
    cached = _access_tokens.get(host)
    if not force_refresh and cached and now < cached[1] - 60:
        return cached[0]
    async with _token_lock:
        now = time.monotonic()
        cached = _access_tokens.get(host)
        if not force_refresh and cached and now < cached[1] - 60:
            return cached[0]
        client = await _get_http_client()
        try:
            async with client.stream(
                "POST",
                f"{host}/oauth/token",
                data={"grant_type": "client_credentials"},
                auth=httpx.BasicAuth(FFLOGS_CLIENT_ID, FFLOGS_CLIENT_SECRET),
            ) as response:
                if response.status_code in {400, 401, 403}:
                    raise FFLogsError("FF Logs Client ID 或 Client Secret 无效。")
                if response.status_code != 200:
                    raise FFLogsError(
                        f"FF Logs OAuth 暂时不可用（HTTP {response.status_code}）。"
                    )
                payload = await _decode_json_response(response)
        except FFLogsError:
            raise
        except httpx.TimeoutException as exc:
            raise FFLogsError("FF Logs OAuth 响应超时，请稍后重试。") from exc
        except httpx.RequestError as exc:
            raise FFLogsError("FF Logs OAuth 连接失败，请稍后重试。") from exc

        token = payload.get("access_token")
        if not isinstance(token, str) or not token or len(token) > 4096:
            raise FFLogsError("FF Logs OAuth 未返回有效访问令牌。")
        expires_in = _finite_number(payload.get("expires_in")) or 3600.0
        _access_tokens[host] = (
            token,
            time.monotonic() + min(max(expires_in, 120.0), 86400.0),
        )
        return token


async def _respect_global_interval() -> None:
    global _last_api_request_at
    if FFLOGS_GLOBAL_INTERVAL <= 0:
        return
    async with _global_rate_lock:
        remaining = FFLOGS_GLOBAL_INTERVAL - (
            time.monotonic() - _last_api_request_at
        )
        if remaining > 0:
            await asyncio.sleep(remaining)
        _last_api_request_at = time.monotonic()


async def _graphql_request(
    query: str,
    variables: Mapping[str, Any],
    host: str,
) -> Mapping[str, Any]:
    error = configuration_error()
    if error:
        raise FFLogsError(error)
    client = await _get_http_client()
    async with _request_slots:
        await _respect_global_interval()
        for attempt in range(2):
            token = await _get_access_token(host, force_refresh=attempt > 0)
            try:
                async with client.stream(
                    "POST",
                    f"{host}/api/v2/client",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"query": query, "variables": dict(variables)},
                ) as response:
                    if response.status_code == 401 and attempt == 0:
                        continue
                    if response.status_code == 429:
                        retry_after = response.headers.get("retry-after", "").strip()
                        wait_text = (
                            f"约 {retry_after} 秒后"
                            if retry_after.isascii()
                            and retry_after.isdigit()
                            and 1 <= int(retry_after) <= 3600
                            else "稍后"
                        )
                        raise FFLogsError(f"FF Logs API 限额已用完，请{wait_text}重试。")
                    if response.status_code in {401, 403}:
                        raise FFLogsError("FF Logs API 鉴权失败，请重新运行 fflogs-setup。")
                    if response.status_code != 200:
                        raise FFLogsError(
                            f"FF Logs API 暂时不可用（HTTP {response.status_code}）。"
                        )
                    payload = await _decode_json_response(response)
            except FFLogsError:
                raise
            except httpx.TimeoutException as exc:
                raise FFLogsError("FF Logs API 响应超时，请稍后重试。") from exc
            except httpx.RequestError as exc:
                raise FFLogsError("FF Logs API 连接失败，请稍后重试。") from exc

            errors = payload.get("errors")
            if isinstance(errors, list) and errors:
                messages = " ".join(
                    str(item.get("message", ""))
                    for item in errors
                    if isinstance(item, Mapping)
                ).lower()
                if "permission" in messages or "hidden" in messages:
                    raise FFLogsError("该角色已隐藏 FF Logs 排名。")
                raise FFLogsError("FF Logs 暂时无法完成这次查询。")
            data = payload.get("data")
            if not isinstance(data, Mapping):
                raise FFLogsError("FF Logs API 未返回查询数据。")
            return data
    raise FFLogsError("FF Logs API 鉴权失败。")


@singleflight(lambda query: (query.name.casefold(), query.server.casefold(), query.region,
                            query.zone_id, query.job, query.metric))
async def query_character_rankings(query: CharacterQuery) -> Mapping[str, Any]:
    key = (
        query.name.casefold(),
        query.server.casefold(),
        query.region,
        query.zone_id,
        query.job,
        query.metric,
    )
    now = time.monotonic()
    if FFLOGS_CACHE_TTL > 0:
        async with _cache_lock:
            cached = _response_cache.get(key)
            if cached and now - cached[0] <= FFLOGS_CACHE_TTL:
                return cached[1]

    data = await _graphql_request(
        CHARACTER_RANKINGS_QUERY,
        {
            "name": query.name,
            "serverSlug": query.server,
            "serverRegion": query.region,
            "zoneID": query.zone_id,
            "specName": query.job,
            "metric": query.metric,
        },
        _host_for_region(query.region),
    )
    character_data = data.get("characterData")
    character = (
        character_data.get("character")
        if isinstance(character_data, Mapping)
        else None
    )
    if character is None:
        raise FFLogsError(
            f"没有找到 {_safe_piece(query.region, 8)} "
            f"区的角色“{_safe_piece(query.name, 64)}”，"
            "请检查角色名和服务器名称（国际服通常使用英文 slug）。"
        )
    if not isinstance(character, Mapping):
        raise FFLogsError("FF Logs 角色数据格式不正确。")

    rate_limit = data.get("rateLimitData")
    if isinstance(rate_limit, Mapping):
        limit = _finite_number(rate_limit.get("limitPerHour"))
        spent = _finite_number(rate_limit.get("pointsSpentThisHour"))
        if limit and spent is not None and spent >= limit * 0.9:
            logger.warning("FF Logs API hourly point budget is at or above 90%")

    if FFLOGS_CACHE_TTL > 0:
        async with _cache_lock:
            if len(_response_cache) >= 200:
                oldest_key = min(
                    _response_cache,
                    key=lambda existing_key: _response_cache[existing_key][0],
                )
                _response_cache.pop(oldest_key, None)
            _response_cache[key] = (time.monotonic(), character)
    return character


async def query_zone_catalog(region: str) -> tuple[Mapping[str, Any], ...]:
    host = _host_for_region(region)
    now = time.monotonic()
    async with _zone_cache_lock:
        cached = _zone_catalog_cache.get(host)
        if cached and now - cached[0] <= ZONE_CACHE_TTL_SECONDS:
            return cached[1]

    data = await _graphql_request(WORLD_ZONES_QUERY, {}, host)
    world_data = data.get("worldData")
    raw_zones = world_data.get("zones") if isinstance(world_data, Mapping) else None
    if not isinstance(raw_zones, list):
        raise FFLogsError("FF Logs 没有返回可用的副本目录。")

    catalog: list[Mapping[str, Any]] = []
    for raw_zone in raw_zones:
        if not isinstance(raw_zone, Mapping):
            continue
        raw_id = _finite_number(raw_zone.get("id"))
        name = _safe_piece(raw_zone.get("name"), 100)
        if raw_id is None or not name:
            continue
        zone_id = int(raw_id)
        encounter_names: list[str] = []
        raw_encounters = raw_zone.get("encounters")
        if isinstance(raw_encounters, list):
            for encounter in raw_encounters:
                if isinstance(encounter, Mapping):
                    encounter_name = _safe_piece(encounter.get("name"), 100)
                    if encounter_name:
                        encounter_names.append(encounter_name)
        catalog.append(
            {
                "id": zone_id,
                "name": name,
                "frozen": bool(raw_zone.get("frozen")),
                "encounters": tuple(encounter_names),
            }
        )

    if not catalog:
        raise FFLogsError("FF Logs 副本目录暂时为空，请稍后重试。")
    result = tuple(catalog)
    async with _zone_cache_lock:
        _zone_catalog_cache[host] = (time.monotonic(), result)
        for zone in result:
            _zone_names[(host, int(zone["id"]))] = str(zone["name"])
    return result


def _zone_search_text(zone: Mapping[str, Any]) -> str:
    encounter_names = zone.get("encounters")
    encounters = (
        " ".join(str(name) for name in encounter_names)
        if isinstance(encounter_names, (list, tuple))
        else ""
    )
    return f"{zone.get('name', '')} {encounters}".casefold()


def _format_zone_rows(zones: list[Mapping[str, Any]], limit: int) -> list[str]:
    ordered = sorted(zones, key=lambda zone: int(zone["id"]), reverse=True)
    rows: list[str] = []
    for zone in ordered[:limit]:
        zone_id = int(zone["id"])
        short_name = _ZONE_SHORT_NAMES.get(zone_id)
        prefix = f"{zone_id} · {short_name}" if short_name else str(zone_id)
        rows.append(f"{prefix}　{zone['name']}")
    return rows


def format_zone_catalog(
    zones: tuple[Mapping[str, Any], ...],
    region: str,
    show_all: bool = False,
) -> str:
    active = [zone for zone in zones if not bool(zone.get("frozen"))]
    lines = [f"区域　{region}　｜　状态　当前排名分区"]
    if show_all:
        lines.extend((DIVIDER, *_format_zone_rows(active, 28)))
        if len(active) > 28:
            lines.append(f"……另有 {len(active) - 28} 个当前分区")
    else:
        categories = (
            (
                "⚔️ 零式",
                ("阿卡狄亚", "万魔殿", "伊甸希望乐园", "零式", "savage", "arcadion"),
            ),
            ("🔥 绝境战", ("绝境", "ultimate")),
            ("🐲 极神 / 高难度", ("高难度", "extreme", "trial")),
        )
        shown_ids: set[int] = set()
        for title, keywords in categories:
            matched = [
                zone
                for zone in active
                if int(zone["id"]) not in shown_ids
                and any(keyword in _zone_search_text(zone) for keyword in keywords)
            ]
            if not matched:
                continue
            lines.extend((DIVIDER, title, *_format_zone_rows(matched, 5)))
            shown_ids.update(int(zone["id"]) for zone in matched[:5])
        if not shown_ids:
            lines.extend((DIVIDER, *_format_zone_rows(active, 15)))
    return _clean_reply(
        panel(
            "FF Logs · 副本目录",
            lines,
            icon="🗺️",
            footer=(
                "使用示例：/dps 角色名 服务器 副本=M9S（也可写副本=73）。"
                "这个编号是 FF Logs 排名分区 ID，不是游戏任务 ID。"
            ),
        )
    )


async def _cooldown_remaining(event: Event) -> float:
    try:
        user_id = event.get_user_id()
    except Exception:
        user_id = "unknown"
    now = time.monotonic()
    async with _cooldown_lock:
        previous = _last_command_by_user.get(user_id, 0.0)
        remaining = 2.0 - (now - previous)
        if remaining <= 0:
            _last_command_by_user[user_id] = now
            if len(_last_command_by_user) > 2000:
                cutoff = now - 60.0
                stale = [
                    key
                    for key, timestamp in _last_command_by_user.items()
                    if timestamp < cutoff
                ]
                for key in stale:
                    _last_command_by_user.pop(key, None)
            return 0.0
        return remaining


async def _run_character_command(
    matcher: type[Matcher],
    event: Event,
    raw_arguments: str,
    command: str,
    formatter: Any,
) -> None:
    parsed, local_error = parse_character_arguments(raw_arguments, command)
    if local_error:
        await matcher.finish(
            local_error if local_error.startswith("🧭") else error_panel(local_error)
        )
    assert parsed is not None
    config_error = public_configuration_error()
    if config_error:
        await matcher.finish(error_panel(config_error))
    remaining = await _cooldown_remaining(event)
    if remaining > 0:
        await matcher.finish(cooldown_panel(remaining))
    try:
        character = await query_character_rankings(parsed)
        try:
            await query_zone_catalog(parsed.region)
        except FFLogsError as exc:
            logger.debug("Could not refresh FF Logs zone names: {}", exc)
        reply = formatter(character, parsed)
    except FFLogsError as exc:
        await matcher.finish(error_panel(public_error_message(
            _clean_reply(str(exc)),
            "FF Logs 查询暂时不可用，请稍后重试或联系管理员。",
        )))
        return
    await matcher.finish(_clean_reply(reply))


FFLOGS_HELP = help_panel(
    "FF Logs 查询中心",
    [
        "📊 /dps 角色名 服务器　伤害排名",
        "🛡️ /raid 角色名 服务器　团本战绩",
        "🗺️ /fflogs zones　查看副本编号",
        "⚙️ /fflogs status　查看配置状态",
        "",
        "常用可选参数",
        "• 副本=M9S（也可写副本=73）　职业=武士",
        "• 区域=国服　指标=rdps",
        "• /reid 与 /raid 相同",
    ],
    footer="直接发送 /dps 或 /raid 可查看完整示例。",
)

fflogs = on_command("fflogs", force_whitespace=True, priority=10, block=True)
dps = on_command(
    "dps", aliases={"dpscheck"}, force_whitespace=True, priority=10, block=True
)
raid = on_command(
    "raid", aliases={"reid"}, force_whitespace=True, priority=10, block=True
)


@fflogs.handle()
async def handle_fflogs(event: Event, args: Message = CommandArg()) -> None:
    arguments = args.extract_plain_text().strip().split()
    action = arguments[0].lower() if arguments else ""
    if action in {"zones", "zone", "副本"}:
        region = FFLOGS_DEFAULT_REGION
        show_all = False
        for argument in arguments[1:]:
            if argument.lower() in {"all", "全部"}:
                show_all = True
                continue
            parsed_region = _normalize_region(argument)
            if parsed_region:
                region = parsed_region
                continue
            await fflogs.finish(
                error_panel(
                    f"无法识别参数“{_safe_piece(argument, 40)}”。",
                    hint="示例：/fflogs zones、/fflogs zones JP 或 /fflogs zones all",
                )
            )
        remaining = await _cooldown_remaining(event)
        if remaining > 0:
            await fflogs.finish(cooldown_panel(remaining))
        try:
            catalog = await query_zone_catalog(region)
        except FFLogsError as exc:
            await fflogs.finish(error_panel(public_error_message(
                _clean_reply(str(exc)),
                "FF Logs 查询暂时不可用，请稍后重试或联系管理员。",
            )))
            return
        await fflogs.finish(format_zone_catalog(catalog, region, show_all))
    if action not in {"", "help", "status"} or len(arguments) > 1:
        await fflogs.finish(
            error_panel(
                "无法识别这个 FF Logs 子命令。",
                hint="可用：/fflogs、/fflogs status、/fflogs zones",
            )
        )
    if action in {"", "help"}:
        await fflogs.finish(FFLOGS_HELP)
    error = public_configuration_error()
    status = "❌ 不可用" if error else "✅ 已就绪"
    zone = FFLOGS_DEFAULT_ZONE_ID or "由 FF Logs 自动选择"
    await fflogs.finish(
        panel(
            "FF Logs · 服务状态",
            [
                f"API　{status}",
                f"默认区域　{FFLOGS_DEFAULT_REGION}",
                f"默认副本　{zone}",
                *( [f"问题　{error}"] if error else [] ),
            ],
            icon="⚙️",
            footer="发送 /fflogs zones 查看当前副本编号。",
        )
    )


@dps.handle()
async def handle_dps(event: Event, args: Message = CommandArg()) -> None:
    await _run_character_command(
        dps,
        event,
        args.extract_plain_text(),
        "dps",
        format_dps_result,
    )


@raid.handle()
async def handle_raid(event: Event, args: Message = CommandArg()) -> None:
    await _run_character_command(
        raid,
        event,
        args.extract_plain_text(),
        "raid",
        format_raid_result,
    )


driver = get_driver()


@driver.on_startup
async def report_fflogs_configuration() -> None:
    error = configuration_error()
    if error:
        logger.warning("FF Logs plugin is not ready: {}", error)
    else:
        logger.info("FF Logs official API v2 plugin is ready")


@driver.on_shutdown
async def close_fflogs_http_client() -> None:
    global _http_client
    if _http_client is not None and not _http_client.is_closed:
        await _http_client.aclose()
    _http_client = None
    _access_tokens.clear()
    _zone_catalog_cache.clear()
    _zone_names.clear()
