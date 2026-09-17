from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from difflib import get_close_matches
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

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
from bot_tools.catalog import command_directory


DEFAULT_API_BASE = "https://xn--v9x.net/api/"
DEFAULT_TIMEOUT_SECONDS = 18.0
DEFAULT_MIN_INTERVAL_SECONDS = 2.0
DEFAULT_GLOBAL_MIN_INTERVAL_SECONDS = 0.25
DEFAULT_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
DEFAULT_MARKET_CACHE_SECONDS = 60.0
MAX_REPLY_CHARACTERS = 1800
MAX_REPLY_BYTES = 1800
MAX_SIMPLE_QUERY_CHARACTERS = 100
MAX_COMMAND_ARGUMENT_CHARACTERS = 180
TRUNCATION_SUFFIX = "\n……内容过长"


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


def _boolean_from_environment(name: str, default: bool = False) -> bool:
    raw_value = os.environ.get(name, "").strip().lower()
    if not raw_value:
        return default
    if raw_value in {"1", "true", "yes", "on"}:
        return True
    if raw_value in {"0", "false", "no", "off"}:
        return False
    logger.warning("Ignoring invalid {} value", name)
    return default


OTTER_API_QQ = os.environ.get("OTTER_API_QQ", "").strip()
OTTER_API_TOKEN = os.environ.get("OTTER_API_TOKEN", "").strip()
OTTER_API_BASE = os.environ.get("OTTER_API_BASE", DEFAULT_API_BASE).strip().rstrip("/") + "/"
OTTER_API_TIMEOUT = _number_from_environment(
    "OTTER_API_TIMEOUT", DEFAULT_TIMEOUT_SECONDS, 3.0, 30.0
)
OTTER_MIN_INTERVAL = _number_from_environment(
    "OTTER_MIN_INTERVAL", DEFAULT_MIN_INTERVAL_SECONDS, 0.0, 30.0
)
OTTER_GLOBAL_MIN_INTERVAL = _number_from_environment(
    "OTTER_GLOBAL_MIN_INTERVAL", DEFAULT_GLOBAL_MIN_INTERVAL_SECONDS, 0.0, 5.0
)
OTTER_INCLUDE_URLS = _boolean_from_environment("OTTER_INCLUDE_URLS", False)
OTTER_MAX_RESPONSE_BYTES = int(
    _number_from_environment(
        "OTTER_MAX_RESPONSE_BYTES",
        DEFAULT_MAX_RESPONSE_BYTES,
        64 * 1024,
        8 * 1024 * 1024,
    )
)

_CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_CQ_CODE_PATTERN = re.compile(r"\[CQ:[^\]]+\]", re.IGNORECASE)
_WEB_URL_PATTERN = re.compile(
    r"(?:(?:https?:)?//|www\.)[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+",
    re.IGNORECASE,
)
_request_slots = asyncio.Semaphore(4)
_cooldown_lock = asyncio.Lock()
_global_rate_lock = asyncio.Lock()
_last_api_request_at = 0.0
_last_request_by_user: dict[str, float] = {}
_http_client: httpx.AsyncClient | None = None
_ALLOWED_REQUEST_TYPES = frozenset(
    {"weather", "search", "quest", "luck", "house"}
)
_UNIVERSALIS_API_BASE = "https://universalis.app/api/v2"
_CN_MARKET_INDEX_PATH = Path(__file__).with_name("data") / "cn_market_items.tsv"
_CHINA_TIMEZONE = timezone(timedelta(hours=8))
_CHINA_MARKET_TARGET = "中国"
_CHINA_MARKET_ALIASES = frozenset(
    {"中国", "china", "国服", "全大区", "国服全大区", "国服全区"}
)
_CHINA_DATA_CENTERS_FALLBACK = ("陆行鸟", "莫古力", "猫小胖", "豆豆柴")
_DATA_CENTER_CACHE_SECONDS = 6 * 60 * 60
_market_cache_lock = asyncio.Lock()
_market_cache = ResponseCache()
_china_data_centers_cache: tuple[float, tuple[str, ...]] | None = None
_market_names_by_id: dict[int, str] | None = None
_market_ids_by_name: dict[str, tuple[int, ...]] | None = None

# Query parameters contain the WebAPI token. Keep HTTP client access logs disabled
# even when the surrounding bot enables verbose application logging.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class OtterApiError(RuntimeError):
    """An expected OtterBot API error safe to show to a QQ user."""


def _configuration_error() -> str | None:
    if not OTTER_API_QQ and not OTTER_API_TOKEN:
        return "尚未配置獭獭 API。请运行 bot.cmd otter-setup（Linux：./bot.sh otter-setup）。"
    if not OTTER_API_QQ or not OTTER_API_TOKEN:
        return "獭獭 API 配置不完整，请重新运行 bot.cmd otter-setup。"
    if not re.fullmatch(r"[0-9]{5,20}", OTTER_API_QQ):
        return "OTTER_API_QQ 必须填写申请 Token 时使用的 5–20 位个人数字 QQ 号。"
    if len(OTTER_API_TOKEN) > 16:
        return "OTTER_API_TOKEN 最多只能包含 16 个字符，请重新配置。"

    parsed = urlsplit(OTTER_API_BASE)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return "OTTER_API_BASE 必须是没有账号、查询参数或片段的 HTTPS 地址。"
    return None


def _public_configuration_error() -> str | None:
    if _configuration_error():
        return "獭獭查询服务暂时不可用，请稍后重试或联系管理员。"
    return None


def _clean_text(
    value: Any,
    maximum: int = MAX_REPLY_CHARACTERS,
    maximum_bytes: int = MAX_REPLY_BYTES,
) -> str:
    text = _CONTROL_CHARACTER_PATTERN.sub("", str(value or ""))
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) <= maximum and len(text.encode("utf-8")) <= maximum_bytes:
        return text

    candidate = text[: max(maximum - len(TRUNCATION_SUFFIX), 0)].rstrip()
    while candidate and len((candidate + TRUNCATION_SUFFIX).encode("utf-8")) > maximum_bytes:
        candidate = candidate[:-1]
    return candidate.rstrip() + TRUNCATION_SUFFIX


def _without_cq_codes(
    value: Any, maximum: int = MAX_REPLY_CHARACTERS
) -> str:
    raw_text = _CONTROL_CHARACTER_PATTERN.sub("", str(value or ""))
    raw_text = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    raw_text = _CQ_CODE_PATTERN.sub("", raw_text)
    if OTTER_API_TOKEN:
        if len(OTTER_API_TOKEN) >= 4 or raw_text.strip() == OTTER_API_TOKEN:
            raw_text = raw_text.replace(OTTER_API_TOKEN, "[凭据已隐藏]")
    if not OTTER_INCLUDE_URLS:
        raw_text = _WEB_URL_PATTERN.sub("", raw_text)
    return _clean_text(raw_text, maximum)


def _feedback_panel(message: str, title: str = "使用帮助") -> str:
    cleaned = _without_cq_codes(message)
    if "用法：" in cleaned or cleaned.startswith("用法"):
        return help_panel(title, cleaned.splitlines())
    if "只负责查询" in cleaned or "不会上传" in cleaned:
        return panel("功能说明", cleaned, icon="ℹ️")
    return error_panel(cleaned)


def _format_mapping(data: Mapping[str, Any]) -> str:
    preferred_fields = (
        ("title", "content", "url")
        if OTTER_INCLUDE_URLS
        else ("title", "content")
    )
    parts = [
        _without_cq_codes(data.get(field))
        for field in preferred_fields
        if data.get(field)
    ]
    if parts:
        return _clean_text("\n".join(dict.fromkeys(parts)))
    return _without_cq_codes(json.dumps(data, ensure_ascii=False, indent=2))


def format_search_result(data: Any, query: str) -> str:
    safe_query = _without_cq_codes(query, 80)
    if data is False or data is None:
        return panel(
            "物品搜索",
            f"📭 没有找到“{safe_query}”",
            icon="🔎",
            footer="请检查物品名称，或减少关键词后重试。",
        )
    if isinstance(data, str):
        result = _without_cq_codes(data)
        body = result or f"📭 没有找到“{safe_query}”"
        return panel("物品搜索", body, icon="🔎", subtitle=f"关键词　{safe_query}")
    if isinstance(data, Mapping):
        result = _format_mapping(data)
    else:
        result = _without_cq_codes(data)
    return panel("物品搜索", result, icon="🔎", subtitle=f"关键词　{safe_query}")


def format_quest_result(data: Any, query: str) -> str:
    if isinstance(data, Mapping) and isinstance(data.get("data"), Mapping):
        data = data["data"]
    if not data:
        return panel(
            "任务查询",
            f"📭 没有找到“{_without_cq_codes(query, 80)}”",
            icon="📜",
            footer="请检查任务名称，或减少关键词后重试。",
        )
    if isinstance(data, Mapping):
        result = _format_mapping(data)
    else:
        result = _without_cq_codes(data)
    return panel(
        "任务查询",
        result,
        icon="📜",
        subtitle=f"关键词　{_without_cq_codes(query, 80)}",
    )


def format_plain_result(data: Any) -> str:
    if isinstance(data, str):
        return _clean_text(_without_cq_codes(data))
    if isinstance(data, Mapping):
        return _format_mapping(data)
    if isinstance(data, list):
        return _without_cq_codes(json.dumps(data, ensure_ascii=False, indent=2))
    return _without_cq_codes(data)


def format_weather_result(
    data: Any, territory: str, weather_name: str = ""
) -> str:
    if not isinstance(data, list):
        return format_plain_result(data)
    subtitle = f"区域　{territory}"
    if weather_name:
        subtitle += f"　｜　筛选　{weather_name}"
    lines: list[str] = []
    for item in data[:15]:
        if not isinstance(item, Mapping):
            continue
        previous = str(item.get("pre_name", "")).strip()
        current = str(item.get("name", "")).strip()
        transition = f"{previous}→{current}" if previous else current
        eorzea_time = str(item.get("ET", "?")).strip()
        local_time = str(item.get("LT", "?")).strip()
        lines.append(f"{transition}｜ET {eorzea_time}｜本地 {local_time}")
    if not lines:
        lines.append("📭 暂无符合条件的天气记录")
    return _without_cq_codes(
        panel(
            "艾欧泽亚天气",
            lines,
            icon="🌤️",
            subtitle=subtitle,
            footer="ET 为艾欧泽亚时间，本地时间按接口返回显示。",
        )
    )


def _argument_length_error(raw_arguments: str) -> str | None:
    if len(raw_arguments) > MAX_COMMAND_ARGUMENT_CHARACTERS:
        return f"参数过长，请控制在 {MAX_COMMAND_ARGUMENT_CHARACTERS} 个字符以内。"
    return None


def _simple_query_error(query: str, usage: str) -> str | None:
    if not query:
        return usage
    if len(query) > MAX_SIMPLE_QUERY_CHARACTERS:
        return f"查询内容过长，请控制在 {MAX_SIMPLE_QUERY_CHARACTERS} 个字符以内。"
    return None


def parse_market_arguments(raw_arguments: str) -> tuple[dict[str, Any] | None, str | None]:
    length_error = _argument_length_error(raw_arguments)
    if length_error:
        return None, length_error
    arguments = raw_arguments.split()
    if not arguments or arguments[0].lower() == "help":
        return None, (
            "用法：/market <物品名或ID> <服务器>\n"
            "国服比价：/market <物品名或ID> 国服全大区\n"
            "也可以使用 /mitem 作为别名。"
        )
    if arguments[0].lower() == "upload":
        return None, (
            "交易数据由 Universalis 等工具提供。此命令只负责查询，"
            "不会从 QQ 上传你的本地游戏数据。"
        )
    if arguments[0].lower() == "item":
        arguments = arguments[1:]
    if len(arguments) < 2:
        return None, (
            "参数不足。用法：/market <物品名或ID> <服务器>；"
            "全区比价可用 /market <物品名或ID> 国服全大区"
        )

    server_name = arguments[-1]
    item_name = " ".join(arguments[:-1]).strip()
    item_tokens = arguments[:-1]
    hq = any(token.lower() == "hq" for token in item_tokens)
    item_name = " ".join(token for token in item_tokens if token.lower() != "hq").strip()
    if not hq and re.search(r"hq$", item_name, flags=re.IGNORECASE):
        hq = True
        item_name = re.sub(r"hq$", "", item_name, count=1, flags=re.IGNORECASE).rstrip()
    if not item_name:
        return None, "物品名不能为空。"
    if len(item_name) > MAX_SIMPLE_QUERY_CHARACTERS or len(server_name) > 40:
        return None, "物品名或服务器名过长，请缩短后重试。"
    if server_name.casefold() in _CHINA_MARKET_ALIASES:
        server_name = _CHINA_MARKET_TARGET
    return {
        "item_name": item_name,
        "server_name": server_name,
        "hq": hq,
    }, None


def _normalise_market_name(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


def _load_market_item_index() -> tuple[dict[int, str], dict[str, tuple[int, ...]]]:
    global _market_names_by_id, _market_ids_by_name
    if _market_names_by_id is not None and _market_ids_by_name is not None:
        return _market_names_by_id, _market_ids_by_name

    names_by_id: dict[int, str] = {}
    ids_by_name: dict[str, list[int]] = {}
    try:
        with _CN_MARKET_INDEX_PATH.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line or line.startswith("#"):
                    continue
                item_id_text, separator, name = line.rstrip("\r\n").partition("\t")
                if not separator or not item_id_text.isascii() or not item_id_text.isdigit():
                    continue
                item_id = int(item_id_text)
                name = name.strip()
                if not name:
                    continue
                names_by_id[item_id] = name
                ids_by_name.setdefault(_normalise_market_name(name), []).append(item_id)
    except OSError as exc:
        raise OtterApiError("本地国服物品索引不可用，请重新构建机器人镜像。") from exc

    if not names_by_id:
        raise OtterApiError("本地国服物品索引为空，请重新构建机器人镜像。")
    _market_names_by_id = names_by_id
    _market_ids_by_name = {
        name: tuple(sorted(item_ids)) for name, item_ids in ids_by_name.items()
    }
    return _market_names_by_id, _market_ids_by_name


def resolve_market_item(item_name: str) -> tuple[int, str]:
    names_by_id, ids_by_name = _load_market_item_index()
    raw_name = item_name.strip()
    if raw_name.isascii() and raw_name.isdigit():
        item_id = int(raw_name)
        if not 1 <= item_id <= 1_000_000:
            raise OtterApiError("物品 ID 必须在 1 到 1000000 之间。")
        return item_id, names_by_id.get(item_id, f"物品 #{item_id}")

    normalised = _normalise_market_name(raw_name)
    exact_ids = ids_by_name.get(normalised, ())
    if exact_ids:
        item_id = exact_ids[0]
        return item_id, names_by_id[item_id]

    contained_names = [name for name in ids_by_name if normalised and normalised in name]
    if len(contained_names) == 1:
        item_id = ids_by_name[contained_names[0]][0]
        return item_id, names_by_id[item_id]

    candidate_names = contained_names[:20]
    if not candidate_names:
        candidate_names = get_close_matches(
            normalised,
            ids_by_name.keys(),
            n=5,
            cutoff=0.55,
        )
    suggestions: list[str] = []
    for candidate in candidate_names:
        display_name = names_by_id[ids_by_name[candidate][0]]
        if display_name not in suggestions:
            suggestions.append(display_name)
        if len(suggestions) >= 5:
            break
    suggestion_text = (
        f"\n可能是：{'、'.join(suggestions)}" if suggestions else ""
    )
    raise OtterApiError(
        f"没有找到可交易物品“{_clean_text(raw_name, 80, 240)}”。"
        f"{suggestion_text}\n也可以直接输入物品 ID，例如 /market 8 拂晓之间。"
    )


def _market_integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result


def format_market_result(
    data: Mapping[str, Any],
    item_name: str,
    server_name: str,
    hq_only: bool,
) -> str:
    raw_listings = data.get("listings")
    listings = raw_listings if isinstance(raw_listings, list) else []
    grouped: dict[tuple[int, bool, str], tuple[int, int]] = {}
    for listing in listings:
        if not isinstance(listing, Mapping):
            continue
        is_hq = bool(listing.get("hq"))
        if hq_only and not is_hq:
            continue
        price = _market_integer(listing.get("pricePerUnit"))
        quantity = _market_integer(listing.get("quantity"))
        if price is None or quantity is None or price < 0 or quantity <= 0:
            continue
        world_name = _clean_text(listing.get("worldName"), 30, 90)
        key = (price, is_hq, world_name)
        previous_quantity, previous_count = grouped.get(key, (0, 0))
        grouped[key] = (previous_quantity + quantity, previous_count + 1)
    rows = sorted(
        (
            (price, quantity, count, is_hq, world_name)
            for (price, is_hq, world_name), (quantity, count) in grouped.items()
        ),
        key=lambda row: (row[0], not row[3], row[4]),
    )

    quality = "　｜　仅 HQ" if hq_only else ""
    subtitle = f"{item_name}　｜　{server_name}{quality}"
    lines: list[str] = []
    if not rows:
        lines.append("📭 暂时没有符合条件的公开挂单")
    else:
        lines.append("当前最低挂单")
        for index, (price, quantity, count, is_hq, world_name) in enumerate(
            rows[:10], start=1
        ):
            pieces = [f"{price:,} 金币/个", f"共 {quantity:,} 个（{count} 单）"]
            if is_hq:
                pieces.append("HQ")
            if world_name and world_name != server_name:
                pieces.append(world_name)
            lines.append(f"{index:02d}　{' · '.join(pieces)}")

    uploaded_at = _market_integer(data.get("lastUploadTime"))
    if uploaded_at and 946684800000 <= uploaded_at <= 4102444800000:
        timestamp = datetime.fromtimestamp(uploaded_at / 1000, tz=_CHINA_TIMEZONE)
        lines.extend((DIVIDER, f"更新于　{timestamp:%m-%d %H:%M:%S}（UTC+8）"))
    return _clean_text(
        panel(
            "市场行情",
            lines,
            icon="🛒",
            subtitle=subtitle,
            footer="Universalis 众包数据，仅供参考；实际价格可能随时变化。",
        )
    )


def _lowest_market_row(
    data: Mapping[str, Any], hq_only: bool
) -> tuple[int, int, tuple[str, ...], str] | None:
    raw_listings = data.get("listings")
    listings = raw_listings if isinstance(raw_listings, list) else []
    valid: list[tuple[int, int, str, bool]] = []
    for listing in listings:
        if not isinstance(listing, Mapping):
            continue
        is_hq = bool(listing.get("hq"))
        if hq_only and not is_hq:
            continue
        price = _market_integer(listing.get("pricePerUnit"))
        quantity = _market_integer(listing.get("quantity"))
        if price is None or quantity is None or price < 0 or quantity <= 0:
            continue
        world_name = _clean_text(listing.get("worldName"), 30, 90)
        valid.append((price, quantity, world_name, is_hq))
    if not valid:
        return None

    lowest_price = min(row[0] for row in valid)
    lowest = [row for row in valid if row[0] == lowest_price]
    quantity = sum(row[1] for row in lowest)
    worlds = tuple(dict.fromkeys(row[2] for row in lowest if row[2]))
    if hq_only or all(row[3] for row in lowest):
        quality = "HQ"
    elif any(row[3] for row in lowest):
        quality = "含 HQ"
    else:
        quality = ""
    return lowest_price, quantity, worlds, quality


def format_china_market_comparison(
    results: Mapping[str, Mapping[str, Any]],
    item_name: str,
    hq_only: bool,
    failed_data_centers: tuple[str, ...] = (),
) -> str:
    available: list[tuple[int, str, int, tuple[str, ...], str]] = []
    empty: list[str] = []
    for data_center, data in results.items():
        row = _lowest_market_row(data, hq_only)
        if row is None:
            empty.append(data_center)
            continue
        price, quantity, worlds, quality = row
        available.append((price, data_center, quantity, worlds, quality))
    available.sort(key=lambda row: (row[0], row[1]))

    lines: list[str] = []
    if available:
        lines.append("四大区当前最低挂单")
        for index, (price, data_center, quantity, worlds, quality) in enumerate(
            available, start=1
        ):
            details = [f"最低价库存 {quantity:,} 个"]
            if worlds:
                world_text = "、".join(worlds[:2])
                if len(worlds) > 2:
                    world_text += f"等 {len(worlds)} 个服务器"
                details.insert(0, world_text)
            if quality:
                details.append(quality)
            lines.append(f"{index:02d}　{data_center}　{price:,} 金币/个")
            lines.append(f"    {' · '.join(details)}")
    else:
        lines.append("📭 国服各大区暂时没有符合条件的公开挂单")

    for data_center in empty:
        lines.append(f"—　{data_center}　暂无符合条件的挂单")
    for data_center in failed_data_centers:
        if data_center not in results:
            lines.append(f"—　{data_center}　本次查询失败")

    quality = "　｜　仅 HQ" if hq_only else ""
    return _clean_text(
        panel(
            "国服全大区比价",
            lines,
            icon="💰",
            subtitle=f"{item_name}　｜　中国区{quality}",
            footer=(
                "按各大区当前最低公开挂单排序；数据来自 Universalis，"
                "价格和库存可能随时变化。"
            ),
        )
    )


def parse_luck_arguments(raw_arguments: str, user_id: str) -> tuple[dict[str, Any] | None, str | None]:
    length_error = _argument_length_error(raw_arguments)
    if length_error:
        return None, length_error
    argument = raw_arguments.strip().lower()
    if argument == "help":
        return None, "用法：/luck 获取今日运势；/luck r 可以重抽。"
    if argument not in {"", "r", "redraw"}:
        return None, "参数无效。用法：/luck 或 /luck r"
    # QQ official events use an OpenID. Do not disclose that identifier to the
    # third-party service; a keyed, stable pseudonym still preserves daily luck.
    key = hashlib.sha256(
        ("nonebot-otter-luck:" + OTTER_API_TOKEN).encode("utf-8")
    ).digest()
    pseudonymous_user_id = hmac.new(
        key, user_id.encode("utf-8"), hashlib.sha256
    ).hexdigest()[:32]
    return {
        "user_id": pseudonymous_user_id,
        "redraw": argument in {"r", "redraw"},
    }, None


def parse_house_arguments(raw_arguments: str) -> tuple[dict[str, Any] | None, str | None]:
    length_error = _argument_length_error(raw_arguments)
    if length_error:
        return None, length_error
    arguments = raw_arguments.split()
    if not arguments or arguments[0].lower() == "help":
        return None, (
            "用法：/house <服务器> [区域] [大小] [部队|个人]\n"
            "示例：/house 萌芽池 海雾村 小 部队"
        )
    if arguments[0].lower() == "upload":
        return None, "此插件只查询獭獭 API，不会上传你的本地房源数据。"

    room_type = next((value for value in ("部队", "个人") if value in arguments), "")
    positional = [value for value in arguments if value not in {"部队", "个人"}]
    if not positional:
        return None, "服务器不能为空。用法：/house <服务器> [区域] [大小] [部队|个人]"
    if len(positional) > 3:
        return None, "参数过多。用法：/house <服务器> [区域] [大小] [部队|个人]"
    if any(len(value) > 40 for value in positional):
        return None, "服务器、区域或大小参数过长，请缩短后重试。"
    return {
        "server": positional[0],
        "area": positional[1] if len(positional) >= 2 else "",
        "size": positional[2] if len(positional) >= 3 else "",
        "r_type": room_type,
    }, None


def parse_weather_arguments(raw_arguments: str) -> tuple[dict[str, Any] | None, str | None]:
    length_error = _argument_length_error(raw_arguments)
    if length_error:
        return None, length_error
    arguments = raw_arguments.split()
    if not arguments or arguments[0].lower() == "help":
        return None, (
            "用法：/weather <区域> [天气] [数量]\n"
            "示例：/weather 优雷卡恒冰之地 暴雪 3"
        )
    if len(arguments) > 3:
        return None, "参数过多。用法：/weather <区域> [天气] [数量]"

    territory = arguments[0]
    weather_name = ""
    length = 5
    if len(arguments) >= 2:
        if arguments[1].isascii() and arguments[1].isdigit():
            length = int(arguments[1])
        else:
            weather_name = arguments[1]
            length = 3
    if len(arguments) == 3:
        if not arguments[2].isascii() or not arguments[2].isdigit():
            return None, "数量必须是 1–15 的整数。"
        length = int(arguments[2])
    if not 1 <= length <= 15:
        return None, "数量必须是 1–15 的整数。"
    if len(territory) > 40 or len(weather_name) > 30:
        return None, "区域或天气名称过长，请缩短后重试。"

    data: dict[str, Any] = {"territory": territory, "length": length}
    if weather_name:
        data["weather"] = weather_name
    return data, None


def roll_dice_expression(raw_expression: str) -> tuple[str | None, str | None]:
    expression = re.sub(r"\s+", "", raw_expression).lower()
    usage = "用法：/dice <NdM表达式>，例如 /dice 3d12+5"
    if not expression or expression == "help":
        return None, usage
    if len(expression) > 80:
        return None, "骰子表达式过长。"
    if not re.fullmatch(
        r"[+-]?(?:[0-9]*d[0-9]+|[0-9]+)(?:[+-](?:[0-9]*d[0-9]+|[0-9]+))*",
        expression,
    ):
        return None, f"无法解析骰子表达式。{usage}"

    total = 0
    total_dice = 0
    details: list[str] = []
    for index, term in enumerate(re.findall(r"[+-]?[^+-]+", expression)):
        sign = -1 if term.startswith("-") else 1
        body = term.lstrip("+-")
        prefix = "-" if sign < 0 else ("+" if index > 0 else "")
        if "d" not in body:
            modifier = int(body)
            if modifier > 1_000_000_000:
                return None, "骰子修正值过大。"
            total += sign * modifier
            details.append(f"{prefix}{modifier}")
            continue

        count_text, sides_text = body.split("d", 1)
        count = int(count_text) if count_text else 1
        sides = int(sides_text)
        if not 1 <= count <= 50 or not 2 <= sides <= 100_000:
            return None, "每项骰子数量须为 1–50，面数须为 2–100000。"
        total_dice += count
        if total_dice > 100:
            return None, "一次最多投掷 100 颗骰子。"
        rolls = [secrets.randbelow(sides) + 1 for _ in range(count)]
        total += sign * sum(rolls)
        roll_text = ",".join(str(value) for value in rolls)
        details.append(f"{prefix}{count}d{sides}[{roll_text}]")
    return f"掷骰：{' '.join(details)}\n结果：{total}", None


async def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=min(5.0, OTTER_API_TIMEOUT),
                read=OTTER_API_TIMEOUT,
                write=min(10.0, OTTER_API_TIMEOUT),
                pool=min(5.0, OTTER_API_TIMEOUT),
            ),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            headers={"Accept": "application/json", "User-Agent": "NoneBot-OtterBridge/1.0"},
            follow_redirects=False,
        )
    return _http_client


async def _respect_global_request_interval() -> None:
    global _last_api_request_at
    if OTTER_GLOBAL_MIN_INTERVAL <= 0:
        return
    async with _global_rate_lock:
        remaining = OTTER_GLOBAL_MIN_INTERVAL - (
            time.monotonic() - _last_api_request_at
        )
        if remaining > 0:
            await asyncio.sleep(remaining)
        _last_api_request_at = time.monotonic()


def _extract_china_data_centers(payload: Any) -> tuple[str, ...]:
    if not isinstance(payload, list):
        return ()
    names: list[str] = []
    for entry in payload:
        if not isinstance(entry, Mapping):
            continue
        region = str(entry.get("region", "")).strip().casefold()
        if region not in {_CHINA_MARKET_TARGET.casefold(), "china"}:
            continue
        name = _clean_text(entry.get("name"), 30, 90)
        if name and name not in names:
            names.append(name)
    return tuple(names)


@singleflight(lambda: "universalis-china-data-centers")
async def get_china_data_centers() -> tuple[str, ...]:
    global _china_data_centers_cache
    now = time.monotonic()
    cached = _china_data_centers_cache
    if cached and now - cached[0] <= _DATA_CENTER_CACHE_SECONDS:
        return cached[1]

    try:
        client = await _get_http_client()
        async with _request_slots:
            await _respect_global_request_interval()
            async with client.stream(
                "GET", f"{_UNIVERSALIS_API_BASE}/data-centers"
            ) as response:
                if response.status_code != 200:
                    raise ValueError(
                        f"Universalis data-center status {response.status_code}"
                    )
                chunks: list[bytes] = []
                received = 0
                async for chunk in response.aiter_bytes():
                    received += len(chunk)
                    if received > 256 * 1024:
                        raise ValueError("Universalis data-center response is too large")
                    chunks.append(chunk)
        payload = json.loads(b"".join(chunks).decode("utf-8-sig"))
        data_centers = _extract_china_data_centers(payload)
        if not data_centers:
            raise ValueError("Universalis returned no China data centers")
    except (httpx.HTTPError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        logger.warning(
            "Unable to refresh Universalis China data centers; using fallback: {}",
            type(exc).__name__,
        )
        data_centers = _CHINA_DATA_CENTERS_FALLBACK

    _china_data_centers_cache = (now, data_centers)
    return data_centers


@singleflight(lambda server_name, item_id, hq_only=False: (server_name.casefold(), item_id, hq_only))
async def call_universalis_market(
    server_name: str,
    item_id: int,
    hq_only: bool = False,
) -> Mapping[str, Any]:
    cache_key = (server_name.casefold(), item_id, hq_only)
    now = time.monotonic()
    async with _market_cache_lock:
        cached = _market_cache.get(cache_key)
        if cached and now - cached[0] <= DEFAULT_MARKET_CACHE_SECONDS:
            return cached[1]

    client = await _get_http_client()
    url = f"{_UNIVERSALIS_API_BASE}/{quote(server_name, safe='')}/{item_id}"
    try:
        async with _request_slots:
            await _respect_global_request_interval()
            params: dict[str, Any] = {"listings": 30, "entries": 0}
            if hq_only:
                params["hq"] = "true"
            async with client.stream(
                "GET",
                url,
                params=params,
            ) as response:
                if response.status_code == 404:
                    raise OtterApiError(
                        "Universalis 没有找到这个服务器或物品，请检查名称。"
                    )
                if response.status_code == 429:
                    raise OtterApiError("Universalis 请求过多，请稍后再试。")
                if response.status_code != 200:
                    raise OtterApiError(
                        f"Universalis 市场接口暂时不可用（HTTP {response.status_code}）。"
                    )

                declared_length = response.headers.get("content-length")
                if declared_length and declared_length.isdigit():
                    if int(declared_length) > OTTER_MAX_RESPONSE_BYTES:
                        raise OtterApiError("Universalis 返回内容过大，已停止处理。")
                chunks: list[bytes] = []
                received = 0
                async for chunk in response.aiter_bytes():
                    received += len(chunk)
                    if received > OTTER_MAX_RESPONSE_BYTES:
                        raise OtterApiError("Universalis 返回内容过大，已停止处理。")
                    chunks.append(chunk)
    except OtterApiError:
        raise
    except httpx.TimeoutException as exc:
        raise OtterApiError("Universalis 市场接口响应超时，请稍后重试。") from exc
    except httpx.RequestError as exc:
        raise OtterApiError("暂时无法连接 Universalis，请稍后重试。") from exc

    try:
        payload = json.loads(b"".join(chunks).decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OtterApiError("Universalis 返回了无法解析的数据。") from exc
    if not isinstance(payload, Mapping):
        raise OtterApiError("Universalis 返回格式不正确。")

    async with _market_cache_lock:
        if len(_market_cache) >= 300:
            oldest_key = min(
                _market_cache,
                key=lambda existing_key: _market_cache[existing_key][0],
            )
            _market_cache.pop(oldest_key, None)
        _market_cache[cache_key] = (time.monotonic(), payload)
    return payload


async def call_china_market_comparison(
    item_id: int, hq_only: bool = False
) -> tuple[dict[str, Mapping[str, Any]], tuple[str, ...]]:
    data_centers = await get_china_data_centers()
    responses = await asyncio.gather(
        *(
            call_universalis_market(data_center, item_id, hq_only)
            for data_center in data_centers
        ),
        return_exceptions=True,
    )
    results: dict[str, Mapping[str, Any]] = {}
    failed: list[str] = []
    for data_center, response in zip(data_centers, responses):
        if isinstance(response, BaseException):
            failed.append(data_center)
        elif isinstance(response, Mapping):
            results[data_center] = response
        else:
            failed.append(data_center)
    if not results:
        raise OtterApiError("Universalis 国服市场接口暂时不可用，请稍后重试。")
    if failed:
        logger.warning(
            "Universalis China comparison was incomplete: {}/{} data centers failed",
            len(failed),
            len(data_centers),
        )
    return results, tuple(failed)


async def call_otter_api(request_type: str, data: Mapping[str, Any]) -> Any:
    if request_type not in _ALLOWED_REQUEST_TYPES:
        raise OtterApiError("该獭獭 WebAPI 请求未启用。")
    error = _configuration_error()
    if error:
        raise OtterApiError(error)

    client = await _get_http_client()
    parameters = {
        "tracker": "webapi",
        "qq": OTTER_API_QQ,
        "token": OTTER_API_TOKEN,
    }
    try:
        async with _request_slots:
            await _respect_global_request_interval()
            async with client.stream(
                "POST",
                OTTER_API_BASE,
                params=parameters,
                json={"request": request_type, "data": dict(data)},
            ) as response:
                if response.status_code != 200:
                    raise OtterApiError(f"獭獭 API 暂时不可用（HTTP {response.status_code}）。")

                declared_length = response.headers.get("content-length")
                if declared_length and declared_length.isdigit():
                    if int(declared_length) > OTTER_MAX_RESPONSE_BYTES:
                        raise OtterApiError("獭獭 API 返回内容过大，已停止处理。")

                chunks: list[bytes] = []
                received = 0
                async for chunk in response.aiter_bytes():
                    received += len(chunk)
                    if received > OTTER_MAX_RESPONSE_BYTES:
                        raise OtterApiError("獭獭 API 返回内容过大，已停止处理。")
                    chunks.append(chunk)
    except OtterApiError:
        raise
    except httpx.TimeoutException as exc:
        raise OtterApiError("獭獭 API 响应超时，请稍后重试。") from exc
    except httpx.RequestError as exc:
        raise OtterApiError("暂时无法连接獭獭 API，请稍后重试。") from exc

    try:
        payload = json.loads(b"".join(chunks).decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OtterApiError("獭獭 API 返回了无法解析的数据。") from exc
    if not isinstance(payload, Mapping):
        raise OtterApiError("獭獭 API 返回格式不正确。")

    response_code = str(payload.get("rcode", ""))
    if response_code != "0":
        if response_code == "100":
            raise OtterApiError("獭獭 API 鉴权参数缺失，请检查服务器配置。")
        if response_code == "101":
            raise OtterApiError("獭獭 API Token 无效，请重新私聊獭獭设置 Token。")
        if response_code == "1041" and request_type == "quest":
            raise OtterApiError("獭獭没有找到对应的 FFXIV 任务。")
        common_errors = {
            "102": "当前獭獭服务不支持这个请求。",
            "103": "獭獭服务无法解析请求。",
            "104": "发给獭獭的请求缺少必要参数。",
            "105": "发给獭獭的参数类型不正确。",
            "-1": "獭獭服务内部发生错误，请稍后重试。",
        }
        if response_code in common_errors:
            raise OtterApiError(common_errors[response_code])
        specific_errors = {
            ("weather", "1001"): "獭獭没有找到这个天气区域。",
            ("weather", "1002"): "獭獭没有找到这个天气名称。",
        }
        specific_error = specific_errors.get((request_type, response_code))
        if specific_error:
            raise OtterApiError(specific_error)
        raise OtterApiError(
            f"獭獭 API 返回未识别错误（{response_code or '?'}），请稍后重试。"
        )
    return payload.get("data")


async def _cooldown_remaining(event: Event) -> float:
    if OTTER_MIN_INTERVAL <= 0:
        return 0.0
    try:
        user_id = event.get_user_id()
    except Exception:
        user_id = "unknown"
    now = time.monotonic()
    async with _cooldown_lock:
        previous = _last_request_by_user.get(user_id, 0.0)
        remaining = OTTER_MIN_INTERVAL - (now - previous)
        if remaining <= 0:
            _last_request_by_user[user_id] = now
            if len(_last_request_by_user) > 2000:
                cutoff = now - max(OTTER_MIN_INTERVAL * 10, 60)
                stale_users = [
                    key for key, timestamp in _last_request_by_user.items() if timestamp < cutoff
                ]
                for key in stale_users:
                    _last_request_by_user.pop(key, None)
            return 0.0
        return remaining


async def _run_request(
    matcher: type[Matcher],
    event: Event,
    request_type: str,
    data: Mapping[str, Any],
    formatter: Callable[[Any], str] = format_plain_result,
) -> None:
    configuration = _public_configuration_error()
    if configuration:
        await matcher.finish(error_panel(configuration))
    remaining = await _cooldown_remaining(event)
    if remaining > 0:
        await matcher.finish(cooldown_panel(remaining))
    try:
        result = await call_otter_api(request_type, data)
        reply = _without_cq_codes(formatter(result))
    except OtterApiError as exc:
        await matcher.finish(error_panel(public_error_message(
            _without_cq_codes(str(exc), 300),
            "獭獭查询服务暂时不可用，请稍后重试或联系管理员。",
        )))
        return
    if not reply:
        reply = panel("暂无结果", "獭獭 API 没有返回可显示的内容。", icon="📭")
    elif formatter is format_plain_result:
        title, icon = {
            "luck": ("今日运势", "🍀"),
            "house": ("房源查询", "🏡"),
        }.get(request_type, ("查询结果", "🔎"))
        reply = panel(title, reply, icon=icon)
    await matcher.finish(reply)


async def _finish_local(matcher: type[Matcher], event: Event, reply: str) -> None:
    remaining = await _cooldown_remaining(event)
    if remaining > 0:
        await matcher.finish(cooldown_panel(remaining))
    await matcher.finish(_without_cq_codes(reply))


OTTER_HELP = command_directory()

otter = on_command("otter", force_whitespace=True, priority=10, block=True)
quest = on_command("quest", force_whitespace=True, priority=10, block=True)
search = on_command("search", force_whitespace=True, priority=10, block=True)
market = on_command(
    "market", aliases={"mitem"}, force_whitespace=True, priority=10, block=True
)
luck = on_command("luck", force_whitespace=True, priority=10, block=True)
house = on_command("house", force_whitespace=True, priority=10, block=True)
weather = on_command(
    "weather", aliases={"天气"}, force_whitespace=True, priority=10, block=True
)
random_roll = on_command("random", force_whitespace=True, priority=10, block=True)
gate = on_command("gate", force_whitespace=True, priority=10, block=True)
dice_roll = on_command("dice", force_whitespace=True, priority=10, block=True)
about = on_command("about", force_whitespace=True, priority=10, block=True)


@otter.handle()
async def handle_otter(event: Event, args: Message = CommandArg()) -> None:
    action = args.extract_plain_text().strip().lower()
    if action == "bots":
        await otter.finish(
            panel(
                "隐私保护",
                "botlist 已停用：旧版接口会返回私有机器人的原始账号信息。",
                icon="🔒",
            )
        )
    if action not in {"", "help", "status"}:
        await otter.finish(
            error_panel(
                "无法识别这个子命令。",
                hint="可用：/otter、/otter status、/otter bots",
            )
        )
    if action in {"", "help"}:
        await otter.finish(OTTER_HELP)
    configuration = _public_configuration_error()
    status = "❌ 不可用" if configuration else "✅ 已就绪"
    link_status = "已开启" if OTTER_INCLUDE_URLS else "已关闭（无需可信域名）"
    await otter.finish(
        panel(
            "獭獭 API · 服务状态",
            [
                f"WebAPI　{status}",
                "市场查询　✅ 已就绪",
                f"外链输出　{link_status}",
                *([f"问题　{configuration}"] if configuration else []),
            ],
            icon="⚙️",
        )
    )


@quest.handle()
async def handle_quest(event: Event, args: Message = CommandArg()) -> None:
    query = args.extract_plain_text().strip()
    query_error = _simple_query_error(query, "用法：/quest <任务名>")
    if query_error:
        await quest.finish(_feedback_panel(query_error, "任务查询"))
    await _run_request(
        quest,
        event,
        "quest",
        {"name": query},
        lambda result: format_quest_result(result, query),
    )


@search.handle()
async def handle_search(event: Event, args: Message = CommandArg()) -> None:
    query = args.extract_plain_text().strip()
    query_error = _simple_query_error(query, "用法：/search <物品名>")
    if query_error:
        await search.finish(_feedback_panel(query_error, "物品搜索"))
    await _run_request(
        search,
        event,
        "search",
        {"name": query},
        lambda result: format_search_result(result, query),
    )


@market.handle()
async def handle_market(event: Event, args: Message = CommandArg()) -> None:
    request_data, local_reply = parse_market_arguments(args.extract_plain_text().strip())
    if local_reply:
        await market.finish(_feedback_panel(local_reply, "市场查询"))
    assert request_data is not None
    remaining = await _cooldown_remaining(event)
    if remaining > 0:
        await market.finish(cooldown_panel(remaining))
    try:
        item_id, item_name = resolve_market_item(str(request_data["item_name"]))
        server_name = str(request_data["server_name"])
        hq_only = bool(request_data.get("hq"))
        if server_name == _CHINA_MARKET_TARGET:
            results, failed = await call_china_market_comparison(item_id, hq_only)
            reply = format_china_market_comparison(
                results, item_name, hq_only, failed
            )
        else:
            result = await call_universalis_market(server_name, item_id, hq_only)
            reply = format_market_result(result, item_name, server_name, hq_only)
    except OtterApiError as exc:
        await market.finish(error_panel(_without_cq_codes(str(exc), 500)))
        return
    await market.finish(_without_cq_codes(reply))


@luck.handle()
async def handle_luck(event: Event, args: Message = CommandArg()) -> None:
    try:
        user_id = event.get_user_id()
    except Exception:
        await luck.finish(error_panel("无法识别当前 QQ 用户，不能计算运势。"))
        return
    request_data, local_reply = parse_luck_arguments(
        args.extract_plain_text().strip(), user_id
    )
    if local_reply:
        await luck.finish(_feedback_panel(local_reply, "今日运势"))
    assert request_data is not None
    await _run_request(luck, event, "luck", request_data)


@house.handle()
async def handle_house(event: Event, args: Message = CommandArg()) -> None:
    request_data, local_reply = parse_house_arguments(args.extract_plain_text().strip())
    if local_reply:
        await house.finish(_feedback_panel(local_reply, "房源查询"))
    assert request_data is not None
    await _run_request(house, event, "house", request_data)


@weather.handle()
async def handle_weather(event: Event, args: Message = CommandArg()) -> None:
    request_data, local_reply = parse_weather_arguments(
        args.extract_plain_text().strip()
    )
    if local_reply:
        await weather.finish(_feedback_panel(local_reply, "天气查询"))
    assert request_data is not None
    territory = str(request_data["territory"])
    weather_name = str(request_data.get("weather", ""))
    await _run_request(
        weather,
        event,
        "weather",
        request_data,
        lambda result: format_weather_result(result, territory, weather_name),
    )


@random_roll.handle()
async def handle_random_roll(event: Event, args: Message = CommandArg()) -> None:
    argument = args.extract_plain_text().strip().lower()
    if argument == "help":
        await random_roll.finish(
            help_panel("随机数", "用法：/random [面数]，默认 1000，最大 1000000。")
        )
    if not argument:
        sides = 1000
    elif argument.isascii() and argument.isdigit():
        sides = int(argument)
    else:
        await random_roll.finish(
            error_panel("面数必须是整数。", hint="用法：/random [面数]")
        )
        return
    if not 1 <= sides <= 1_000_000:
        await random_roll.finish(error_panel("面数必须在 1 到 1000000 之间。"))
    value = secrets.randbelow(sides) + 1
    await _finish_local(
        random_roll,
        event,
        panel("随机数", f"🎯 {value}", icon="🎲", footer=f"范围：1–{sides}"),
    )


@gate.handle()
async def handle_gate(event: Event, args: Message = CommandArg()) -> None:
    argument = args.extract_plain_text().strip().lower()
    if argument in {"", "2"}:
        choices = ("左边", "右边")
    elif argument == "3":
        choices = ("左边", "中间", "右边")
    else:
        await gate.finish(
            help_panel("挖宝选门", "用法：/gate [2|3]，让獭獭帮你选择挖宝门。")
        )
        return
    choice = secrets.choice(choices)
    await _finish_local(
        gate,
        event,
        panel("挖宝选门", f"👉 {choice}门", icon="🚪", footer="祝你一门到底！"),
    )


@dice_roll.handle()
async def handle_dice_roll(event: Event, args: Message = CommandArg()) -> None:
    result, local_reply = roll_dice_expression(args.extract_plain_text().strip())
    if local_reply:
        await dice_roll.finish(_feedback_panel(local_reply, "掷骰子"))
    assert result is not None
    detail, _, total = result.partition("\n结果：")
    await _finish_local(
        dice_roll,
        event,
        panel(
            "掷骰结果",
            [detail.replace("掷骰：", "明细　"), f"结果　{total}"],
            icon="🎲",
        ),
    )


@about.handle()
async def handle_about(event: Event, args: Message = CommandArg()) -> None:
    if args.extract_plain_text().strip():
        await about.finish(help_panel("关于", "用法：/about"))
    lines = [
        "OtterBot 是 Bluefissure 开源的 FFXIV QQ 机器人项目。",
        "当前机器人使用 NoneBot Adapter QQ 兼容插件。",
    ]
    if OTTER_INCLUDE_URLS:
        lines.append("https://github.com/Bluefissure/OtterBot")
    await _finish_local(about, event, panel("关于机器人", lines, icon="🤖"))


driver = get_driver()


@driver.on_startup
async def start_otter_http_client() -> None:
    await _get_http_client()
    try:
        names_by_id, _ = _load_market_item_index()
    except OtterApiError as exc:
        logger.error("Universalis market plugin is not ready: {}", exc)
    else:
        logger.info(
            "Universalis market plugin is ready with {} CN item names",
            len(names_by_id),
        )
    configuration = _configuration_error()
    if configuration:
        logger.warning("Some OtterBot WebAPI commands are not ready: {}", configuration)
        return
    logger.info("OtterBot FFXIV WebAPI plugin is ready")


@driver.on_shutdown
async def close_otter_http_client() -> None:
    global _http_client, _china_data_centers_cache
    if _http_client is not None and not _http_client.is_closed:
        await _http_client.aclose()
    _http_client = None
    _market_cache.clear()
    _china_data_centers_cache = None
