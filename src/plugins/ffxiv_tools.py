from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx
from nonebot import get_driver, on_command
from nonebot.adapters import Event, Message
from nonebot.log import logger
from nonebot.matcher import Matcher
from nonebot.params import CommandArg

from message_ui import DIVIDER, cooldown_panel, error_panel, help_panel, panel
from bot_tools.catalog import ff14_directory
from bot_tools.request_cache import ResponseCache, singleflight


XIVAPI_BASE = "https://xivapi-v2.xivcdn.com/api"
UNIVERSALIS_BASE = "https://universalis.app/api/v2"
HTTP_TIMEOUT_SECONDS = 20.0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_REPLY_BYTES = 1800
STATIC_CACHE_SECONDS = 6 * 60 * 60
MARKET_CACHE_SECONDS = 60
MIN_USER_INTERVAL_SECONDS = 2.0
CHINA_TIMEZONE = timezone(timedelta(hours=8))

_CONTROL_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_CQ_PATTERN = re.compile(r"\[CQ:[^\]]+\]", re.IGNORECASE)
_request_slots = asyncio.Semaphore(4)
_cache_lock = asyncio.Lock()
_cooldown_lock = asyncio.Lock()
_response_cache = ResponseCache(max_entries=500)
_last_request_by_user: dict[str, float] = {}
_http_client: httpx.AsyncClient | None = None

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class FFXIVToolsError(RuntimeError):
    """An expected error that is safe to display to a QQ user."""


@dataclass(frozen=True)
class ItemInfo:
    item_id: int
    name: str


@dataclass(frozen=True)
class Ingredient:
    item_id: int
    name: str
    amount: int


@dataclass(frozen=True)
class RecipeInfo:
    recipe_id: int
    item_id: int
    item_name: str
    amount_result: int
    craft_type: str
    level: int
    stars: int
    ingredients: tuple[Ingredient, ...]


@dataclass(frozen=True)
class GatheringSource:
    base_id: int
    method: str
    node_level: int
    item_level: int
    hidden: bool
    locations: tuple[str, ...]


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _clean_text(value: Any, maximum: int = 120) -> str:
    text = _CONTROL_PATTERN.sub("", str(value or ""))
    text = _CQ_PATTERN.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return text[:maximum].rstrip()


def _limit_reply(value: str) -> str:
    text = _clean_text(value, 4000)
    suffix = "\n……内容过长"
    if len(text.encode("utf-8")) <= MAX_REPLY_BYTES:
        return text
    candidate = text
    while candidate and len((candidate + suffix).encode("utf-8")) > MAX_REPLY_BYTES:
        candidate = candidate[:-1]
    return candidate.rstrip() + suffix


def _nested_fields(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    fields = value.get("fields")
    return fields if isinstance(fields, Mapping) else {}


def _relation_name(value: Any) -> str:
    return _clean_text(_nested_fields(value).get("Name"), 80)


def _validate_item_argument(raw: str, usage: str) -> tuple[str | None, str | None]:
    query = raw.strip()
    if not query or query.casefold() == "help":
        return None, usage
    if len(query) > 80:
        return None, "物品名过长，请控制在 80 个字符以内。"
    if _CONTROL_PATTERN.search(query) or _CQ_PATTERN.search(query):
        return None, "物品名包含不支持的控制内容。"
    if '"' in query or "\\" in query:
        return None, "物品名不能包含引号或反斜杠；也可以直接输入物品 ID。"
    return query, None


def parse_item_command(raw: str, command: str) -> tuple[str | None, str | None]:
    return _validate_item_argument(raw, f"用法：/{command} <物品名或ID>")


def parse_market_command(
    raw: str, command: str
) -> tuple[tuple[str, str] | None, str | None]:
    arguments = raw.strip()
    usage = f"用法：/{command} <物品名或ID> <服务器/大区/区域>"
    if not arguments or arguments.casefold() == "help":
        return None, usage
    if len(arguments) > 130:
        return None, "参数过长，请控制在 130 个字符以内。"
    parts = arguments.rsplit(maxsplit=1)
    if len(parts) != 2:
        return None, f"参数不足。{usage}"
    item_query, target = parts[0].strip(), parts[1].strip()
    item_query, item_error = _validate_item_argument(item_query, usage)
    if item_error:
        return None, item_error
    if not target or len(target) > 40 or any(char in target for char in "/\\?#"):
        return None, "服务器/大区/区域名称无效，请检查后重试。"
    assert item_query is not None
    return (item_query, target), None


async def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=5.0,
                read=HTTP_TIMEOUT_SECONDS,
                write=10.0,
                pool=5.0,
            ),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            headers={
                "Accept": "application/json",
                "User-Agent": "NoneBot-FFXIVTools/1.0",
            },
            follow_redirects=False,
        )
    return _http_client


@singleflight(lambda url, **kw: (url, tuple(sorted((str(k),str(v)) for k,v in (kw.get("params") or {}).items()))))
async def _request_json(
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    cache_seconds: int = 0,
    service_name: str,
) -> Mapping[str, Any]:
    normalised_params = tuple(
        sorted((str(key), str(value)) for key, value in (params or {}).items())
    )
    cache_key = (url, normalised_params)
    now = time.monotonic()
    if cache_seconds > 0:
        async with _cache_lock:
            cached = _response_cache.get(cache_key)
            if cached and now - cached[0] <= cache_seconds:
                return cached[1]

    client = await _get_http_client()
    try:
        async with _request_slots:
            async with client.stream("GET", url, params=params) as response:
                if response.status_code == 404:
                    raise FFXIVToolsError(f"{service_name} 没有找到对应数据。")
                if response.status_code == 429:
                    raise FFXIVToolsError(f"{service_name} 请求过多，请稍后再试。")
                if response.status_code != 200:
                    raise FFXIVToolsError(
                        f"{service_name} 暂时不可用（HTTP {response.status_code}）。"
                    )
                declared_length = response.headers.get("content-length", "")
                if declared_length.isdigit() and int(declared_length) > MAX_RESPONSE_BYTES:
                    raise FFXIVToolsError(f"{service_name} 返回内容过大，已停止处理。")
                chunks: list[bytes] = []
                received = 0
                async for chunk in response.aiter_bytes():
                    received += len(chunk)
                    if received > MAX_RESPONSE_BYTES:
                        raise FFXIVToolsError(
                            f"{service_name} 返回内容过大，已停止处理。"
                        )
                    chunks.append(chunk)
    except FFXIVToolsError:
        raise
    except httpx.TimeoutException as exc:
        raise FFXIVToolsError(f"{service_name} 响应超时，请稍后重试。") from exc
    except httpx.RequestError as exc:
        raise FFXIVToolsError(f"暂时无法连接 {service_name}，请稍后重试。") from exc

    try:
        payload = json.loads(b"".join(chunks).decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FFXIVToolsError(f"{service_name} 返回了无法解析的数据。") from exc
    if not isinstance(payload, Mapping):
        raise FFXIVToolsError(f"{service_name} 返回格式不正确。")

    if cache_seconds > 0:
        async with _cache_lock:
            if len(_response_cache) >= 500:
                oldest = min(_response_cache, key=lambda key: _response_cache[key][0])
                _response_cache.pop(oldest, None)
            _response_cache[cache_key] = (time.monotonic(), payload)
    return payload


def _search_results(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        return []
    return [value for value in raw_results if isinstance(value, Mapping)]


async def resolve_item(query: str) -> ItemInfo:
    if query.isascii() and query.isdigit():
        item_id = int(query)
        if not 1 <= item_id <= 1_000_000:
            raise FFXIVToolsError("物品 ID 必须在 1 到 1000000 之间。")
        payload = await _request_json(
            f"{XIVAPI_BASE}/sheet/Item/{item_id}",
            params={"fields": "Name", "language": "chs"},
            cache_seconds=STATIC_CACHE_SECONDS,
            service_name="中文 XIVAPI",
        )
        name = _clean_text(payload.get("fields", {}).get("Name") if isinstance(payload.get("fields"), Mapping) else "", 80)
        if not name:
            raise FFXIVToolsError(f"中文 XIVAPI 没有找到物品 #{item_id}。")
        return ItemInfo(item_id, name)

    exact_payload = await _request_json(
        f"{XIVAPI_BASE}/search",
        params={
            "sheets": "Item",
            "query": f'Name="{query}"',
            "fields": "Name",
            "language": "chs",
            "limit": 10,
        },
        cache_seconds=STATIC_CACHE_SECONDS,
        service_name="中文 XIVAPI",
    )
    exact_matches: list[ItemInfo] = []
    for result in _search_results(exact_payload):
        row_id = _integer(result.get("row_id"))
        fields = result.get("fields")
        name = _clean_text(fields.get("Name"), 80) if isinstance(fields, Mapping) else ""
        if row_id and name and name.casefold() == query.casefold():
            exact_matches.append(ItemInfo(row_id, name))
    if exact_matches:
        return exact_matches[0]

    suggestion_payload = await _request_json(
        f"{XIVAPI_BASE}/search",
        params={
            "sheets": "Item",
            "query": f'Name~"{query}"',
            "fields": "Name",
            "language": "chs",
            "limit": 5,
        },
        cache_seconds=STATIC_CACHE_SECONDS,
        service_name="中文 XIVAPI",
    )
    suggestions: list[str] = []
    for result in _search_results(suggestion_payload):
        fields = result.get("fields")
        name = _clean_text(fields.get("Name"), 80) if isinstance(fields, Mapping) else ""
        if name and name not in suggestions:
            suggestions.append(name)
    hint = f"\n可能是：{'、'.join(suggestions)}" if suggestions else ""
    raise FFXIVToolsError(f"没有找到物品“{_clean_text(query, 80)}”。{hint}")


def _parse_recipe(result: Mapping[str, Any], fallback_item: ItemInfo) -> RecipeInfo | None:
    recipe_id = _integer(result.get("row_id"))
    fields = result.get("fields")
    if not recipe_id or not isinstance(fields, Mapping):
        return None
    item_relation = fields.get("ItemResult")
    item_id = _integer(item_relation.get("row_id")) if isinstance(item_relation, Mapping) else None
    item_name = _relation_name(item_relation) or fallback_item.name
    amount_result = _integer(fields.get("AmountResult")) or 1
    craft_type = _relation_name(fields.get("CraftType")) or "制作职业"
    level_fields = _nested_fields(fields.get("RecipeLevelTable"))
    level = _integer(level_fields.get("ClassJobLevel")) or 0
    stars = max(0, min(_integer(level_fields.get("Stars")) or 0, 10))

    raw_items = fields.get("Ingredient")
    raw_amounts = fields.get("AmountIngredient")
    if not isinstance(raw_items, list) or not isinstance(raw_amounts, list):
        return None
    ingredients: list[Ingredient] = []
    for relation, raw_amount in zip(raw_items, raw_amounts, strict=False):
        if not isinstance(relation, Mapping):
            continue
        ingredient_id = _integer(relation.get("row_id"))
        amount = _integer(raw_amount)
        name = _relation_name(relation)
        if ingredient_id and ingredient_id > 0 and amount and amount > 0 and name:
            ingredients.append(Ingredient(ingredient_id, name, amount))
    if not ingredients:
        return None
    return RecipeInfo(
        recipe_id=recipe_id,
        item_id=item_id or fallback_item.item_id,
        item_name=item_name,
        amount_result=max(amount_result, 1),
        craft_type=craft_type,
        level=level,
        stars=stars,
        ingredients=tuple(ingredients),
    )


async def fetch_recipes(item: ItemInfo) -> tuple[RecipeInfo, ...]:
    payload = await _request_json(
        f"{XIVAPI_BASE}/search",
        params={
            "sheets": "Recipe",
            "query": f"ItemResult={item.item_id}",
            "fields": (
                "ItemResult.Name,AmountResult,CraftType.Name,"
                "RecipeLevelTable.ClassJobLevel,RecipeLevelTable.Stars,"
                "Ingredient[].Name,AmountIngredient[]"
            ),
            "language": "chs",
            "limit": 10,
        },
        cache_seconds=STATIC_CACHE_SECONDS,
        service_name="中文 XIVAPI",
    )
    recipes = [
        parsed
        for result in _search_results(payload)
        if (parsed := _parse_recipe(result, item)) is not None
    ]
    recipes.sort(key=lambda recipe: (recipe.level, recipe.recipe_id))
    return tuple(recipes)


def format_recipes(item: ItemInfo, recipes: Sequence[RecipeInfo]) -> str:
    lines: list[str] = []
    for index, recipe in enumerate(recipes[:3], start=1):
        if index > 1:
            lines.append(DIVIDER)
        star_text = f" {'★' * recipe.stars}" if recipe.stars else ""
        lines.append(
            f"🛠️ 配方 {index}　{recipe.craft_type} Lv.{recipe.level}{star_text}"
        )
        lines.append(f"产出　{recipe.item_name} ×{recipe.amount_result}")
        lines.append("材料")
        lines.extend(f"  • {ingredient.name} ×{ingredient.amount}" for ingredient in recipe.ingredients)
    footer = "数据来自当前国服客户端表。"
    if len(recipes) > 3:
        footer = f"共找到 {len(recipes)} 种配方，仅显示前 3 种；{footer}"
    return _limit_reply(
        panel(
            "制作配方",
            lines,
            icon="📖",
            subtitle=f"{item.name}　｜　物品 ID {item.item_id}",
            footer=footer,
        )
    )


async def _fetch_gathering_points(base_id: int) -> tuple[str, ...]:
    payload = await _request_json(
        f"{XIVAPI_BASE}/search",
        params={
            "sheets": "GatheringPoint",
            "query": f"GatheringPointBase={base_id}",
            "fields": (
                "PlaceName.Name,TerritoryType.PlaceName.Name,"
                "GatheringSubCategory.Name"
            ),
            "language": "chs",
            "limit": 50,
        },
        cache_seconds=STATIC_CACHE_SECONDS,
        service_name="中文 XIVAPI",
    )
    locations: list[str] = []
    for result in _search_results(payload):
        fields = result.get("fields")
        if not isinstance(fields, Mapping):
            continue
        territory_fields = _nested_fields(fields.get("TerritoryType"))
        territory = _relation_name(territory_fields.get("PlaceName"))
        place = _relation_name(fields.get("PlaceName"))
        subcategory = _relation_name(fields.get("GatheringSubCategory"))
        pieces = [value for value in (territory, place, subcategory) if value]
        location = " · ".join(dict.fromkeys(pieces))
        if location and location not in locations:
            locations.append(location)
    return tuple(locations)


async def fetch_gathering_sources(item: ItemInfo) -> tuple[GatheringSource, ...]:
    gathering_payload = await _request_json(
        f"{XIVAPI_BASE}/search",
        params={
            "sheets": "GatheringItem",
            "query": f"Item={item.item_id}",
            "fields": "GatheringItemLevel.GatheringItemLevel,IsHidden",
            "language": "chs",
            "limit": 20,
        },
        cache_seconds=STATIC_CACHE_SECONDS,
        service_name="中文 XIVAPI",
    )
    gathering_items: list[tuple[int, int, bool]] = []
    for result in _search_results(gathering_payload):
        gathering_id = _integer(result.get("row_id"))
        fields = result.get("fields")
        if not gathering_id or not isinstance(fields, Mapping):
            continue
        level = _integer(
            _nested_fields(fields.get("GatheringItemLevel")).get("GatheringItemLevel")
        ) or 0
        gathering_items.append((gathering_id, level, bool(fields.get("IsHidden"))))
    if not gathering_items:
        return ()

    async def fetch_bases(
        gathering_id: int, item_level: int, hidden: bool
    ) -> list[tuple[int, str, int, int, bool]]:
        payload = await _request_json(
            f"{XIVAPI_BASE}/search",
            params={
                "sheets": "GatheringPointBase",
                "query": f"Item[]={gathering_id}",
                "fields": "GatheringType.Name,GatheringLevel",
                "language": "chs",
                "limit": 20,
            },
            cache_seconds=STATIC_CACHE_SECONDS,
            service_name="中文 XIVAPI",
        )
        bases: list[tuple[int, str, int, int, bool]] = []
        for result in _search_results(payload):
            base_id = _integer(result.get("row_id"))
            fields = result.get("fields")
            if not base_id or not isinstance(fields, Mapping):
                continue
            method = _relation_name(fields.get("GatheringType")) or "采集"
            node_level = _integer(fields.get("GatheringLevel")) or item_level
            bases.append((base_id, method, node_level, item_level, hidden))
        return bases

    base_groups = await asyncio.gather(
        *(fetch_bases(*gathering_item) for gathering_item in gathering_items[:8])
    )
    unique_bases: dict[int, tuple[int, str, int, int, bool]] = {}
    for base_group in base_groups:
        for base in base_group:
            unique_bases.setdefault(base[0], base)
    limited_bases = list(unique_bases.values())[:12]
    location_groups = await asyncio.gather(
        *(_fetch_gathering_points(base[0]) for base in limited_bases)
    )

    sources: list[GatheringSource] = []
    seen: set[tuple[str, int, int, bool, tuple[str, ...]]] = set()
    for base, locations in zip(limited_bases, location_groups, strict=True):
        base_id, method, node_level, item_level, hidden = base
        key = (method, node_level, item_level, hidden, locations)
        if key in seen:
            continue
        seen.add(key)
        sources.append(
            GatheringSource(
                base_id, method, node_level, item_level, hidden, locations
            )
        )
    return tuple(sources)


def format_gathering(item: ItemInfo, sources: Sequence[GatheringSource]) -> str:
    lines: list[str] = []
    for index, source in enumerate(sources[:8], start=1):
        tags = [f"物品 Lv.{source.item_level}"] if source.item_level else []
        if source.hidden:
            tags.append("隐藏采集物")
        suffix = f"　｜　{' · '.join(tags)}" if tags else ""
        lines.append(f"{index:02d}　{source.method} Lv.{source.node_level}{suffix}")
        if source.locations:
            lines.extend(f"    📍 {location}" for location in source.locations[:4])
        else:
            lines.append("    📍 特殊区域或未公开具体地点")
    return _limit_reply(
        panel(
            "采集地点",
            lines,
            icon="⛏️",
            subtitle=f"{item.name}　｜　物品 ID {item.item_id}",
            footer="地点来自当前国服游戏数据；限时、传说与特殊条件请以游戏内采集手册为准。",
        )
    )


async def fetch_sales(target: str, item_id: int) -> Mapping[str, Any]:
    return await _request_json(
        f"{UNIVERSALIS_BASE}/history/{quote(target, safe='')}/{item_id}",
        params={"entriesToReturn": 20},
        cache_seconds=MARKET_CACHE_SECONDS,
        service_name="Universalis",
    )


def format_sales(item: ItemInfo, target: str, payload: Mapping[str, Any]) -> str:
    entries = payload.get("entries")
    raw_entries = entries if isinstance(entries, list) else []
    sales_rows: list[tuple[int, int, int, bool, str]] = []
    for entry in raw_entries:
        if not isinstance(entry, Mapping):
            continue
        price = _integer(entry.get("pricePerUnit"))
        quantity = _integer(entry.get("quantity"))
        timestamp = _integer(entry.get("timestamp"))
        world = _clean_text(entry.get("worldName"), 30)
        if price is None or price < 0 or not quantity or quantity <= 0 or not timestamp:
            continue
        sales_rows.append((timestamp, price, quantity, bool(entry.get("hq")), world))
    sales_rows.sort(key=lambda row: row[0], reverse=True)

    lines: list[str] = []
    if not sales_rows:
        lines.append("📭 暂时没有公开成交记录")
    else:
        total_units = sum(row[2] for row in sales_rows)
        weighted_total = sum(row[1] * row[2] for row in sales_rows)
        average = round(weighted_total / total_units) if total_units else 0
        lines.extend(
            (
                f"样本均价　{average:,} 金币/个",
                f"样本数量　{total_units:,} 个 · {len(sales_rows)} 笔",
                DIVIDER,
                "最近成交",
            )
        )
        for timestamp, price, quantity, is_hq, world in sales_rows[:8]:
            moment = datetime.fromtimestamp(timestamp, tz=CHINA_TIMEZONE)
            details = [f"{price:,}/个", f"×{quantity:,}"]
            if is_hq:
                details.append("HQ")
            if world and world != target:
                details.append(world)
            lines.append(f"{moment:%m-%d %H:%M}　{' · '.join(details)}")
        velocity = _number(payload.get("regularSaleVelocity"))
        if velocity is not None and velocity > 0:
            lines.extend((DIVIDER, f"参考售速　约 {velocity:,.1f} 个/天"))
    return _limit_reply(
        panel(
            "历史成交",
            lines,
            icon="🧾",
            subtitle=f"{item.name}　｜　{target}",
            footer="仅统计 Universalis 收录的最近公开成交；不会显示买家姓名。",
        )
    )


async def fetch_current_market(
    target: str, item_ids: Sequence[int], *, listings: int = 50
) -> Mapping[str, Any]:
    clean_ids = tuple(dict.fromkeys(item_id for item_id in item_ids if item_id > 0))
    if not clean_ids or len(clean_ids) > 100:
        raise FFXIVToolsError("市场物品数量无效。")
    joined_ids = ",".join(str(item_id) for item_id in clean_ids)
    return await _request_json(
        f"{UNIVERSALIS_BASE}/{quote(target, safe='')}/{joined_ids}",
        params={"listings": max(1, min(listings, 100)), "entries": 0},
        cache_seconds=MARKET_CACHE_SECONDS,
        service_name="Universalis",
    )


def _market_item_map(payload: Mapping[str, Any]) -> dict[int, Mapping[str, Any]]:
    raw_items = payload.get("items")
    result: dict[int, Mapping[str, Any]] = {}
    if isinstance(raw_items, Mapping):
        for key, value in raw_items.items():
            item_id = _integer(key)
            if item_id and isinstance(value, Mapping):
                result[item_id] = value
        return result
    item_id = _integer(payload.get("itemID"))
    if item_id:
        result[item_id] = payload
    return result


def _valid_listings(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw_listings = payload.get("listings")
    if not isinstance(raw_listings, list):
        return []
    listings: list[Mapping[str, Any]] = []
    for listing in raw_listings:
        if not isinstance(listing, Mapping):
            continue
        price = _integer(listing.get("pricePerUnit"))
        quantity = _integer(listing.get("quantity"))
        if price is not None and price >= 0 and quantity and quantity > 0:
            listings.append(listing)
    return listings


def _lowest_unit_price(payload: Mapping[str, Any]) -> int | None:
    prices = [
        price
        for listing in _valid_listings(payload)
        if (price := _integer(listing.get("pricePerUnit"))) is not None
    ]
    return min(prices) if prices else None


def format_cheapest(item: ItemInfo, target: str, payload: Mapping[str, Any]) -> str:
    item_payload = _market_item_map(payload).get(item.item_id, payload)
    listings = _valid_listings(item_payload)
    best_by_world: dict[str, tuple[int, int, bool]] = {}
    for listing in listings:
        price = _integer(listing.get("pricePerUnit"))
        quantity = _integer(listing.get("quantity"))
        if price is None or quantity is None:
            continue
        world = _clean_text(listing.get("worldName"), 30) or target
        is_hq = bool(listing.get("hq"))
        previous = best_by_world.get(world)
        if previous is None or price < previous[0]:
            best_by_world[world] = (price, quantity, is_hq)
        elif price == previous[0]:
            best_by_world[world] = (
                price,
                previous[1] + quantity,
                previous[2] and is_hq,
            )
    rows = sorted(
        ((price, world, quantity, is_hq) for world, (price, quantity, is_hq) in best_by_world.items()),
        key=lambda row: (row[0], row[1]),
    )
    lines: list[str] = []
    if not rows:
        lines.append("📭 暂时没有公开挂单")
    else:
        lines.append("各服务器最低单价")
        for index, (price, world, quantity, is_hq) in enumerate(rows[:10], start=1):
            quality = " · HQ" if is_hq else ""
            lines.append(
                f"{index:02d}　{world}　{price:,}/个 · 最低价库存 {quantity:,}{quality}"
            )
    return _limit_reply(
        panel(
            "跨服比价",
            lines,
            icon="💰",
            subtitle=f"{item.name}　｜　查询范围 {target}",
            footer="按当前公开挂单的单价排序；价格和库存可能随时变化。",
        )
    )


@dataclass(frozen=True)
class RecipeEstimate:
    recipe: RecipeInfo
    total: int
    missing: tuple[Ingredient, ...]
    material_rows: tuple[tuple[Ingredient, int | None], ...]


def estimate_recipe(
    recipe: RecipeInfo, markets: Mapping[int, Mapping[str, Any]]
) -> RecipeEstimate:
    rows: list[tuple[Ingredient, int | None]] = []
    missing: list[Ingredient] = []
    total = 0
    for ingredient in recipe.ingredients:
        price = _lowest_unit_price(markets.get(ingredient.item_id, {}))
        rows.append((ingredient, price))
        if price is None:
            missing.append(ingredient)
        else:
            total += price * ingredient.amount
    return RecipeEstimate(recipe, total, tuple(missing), tuple(rows))


def format_craft_cost(
    item: ItemInfo,
    target: str,
    estimates: Sequence[RecipeEstimate],
    markets: Mapping[int, Mapping[str, Any]],
) -> str:
    complete = [estimate for estimate in estimates if not estimate.missing]
    if complete:
        selected = min(complete, key=lambda estimate: estimate.total)
    else:
        selected = min(estimates, key=lambda estimate: (len(estimate.missing), estimate.total))
    recipe = selected.recipe
    star_text = f" {'★' * recipe.stars}" if recipe.stars else ""
    lines = [
        f"制作　{recipe.craft_type} Lv.{recipe.level}{star_text} · 产出 ×{recipe.amount_result}",
        DIVIDER,
        "材料估价",
    ]
    for ingredient, price in selected.material_rows:
        if price is None:
            lines.append(f"  • {ingredient.name} ×{ingredient.amount}　暂无行情")
        else:
            subtotal = price * ingredient.amount
            lines.append(
                f"  • {ingredient.name} ×{ingredient.amount}　{price:,}/个 → {subtotal:,}"
            )
    lines.append(DIVIDER)
    if selected.missing:
        lines.append(f"已知材料小计　{selected.total:,} 金币")
        lines.append("⚠️ 有材料缺少行情，无法计算完整成本和差额")
    else:
        per_item_cost = selected.total / recipe.amount_result
        lines.append(f"每炉材料成本　{selected.total:,} 金币")
        lines.append(f"折合成品成本　{per_item_cost:,.0f} 金币/个")
        result_price = _lowest_unit_price(markets.get(recipe.item_id, {}))
        if result_price is None:
            lines.append("成品最低价　暂无行情")
        else:
            revenue = result_price * recipe.amount_result
            difference = revenue - selected.total
            sign = "+" if difference >= 0 else ""
            lines.append(f"成品最低价　{result_price:,} 金币/个")
            lines.append(f"每炉价差　{sign}{difference:,} 金币（未扣税费）")
    if len(estimates) > 1:
        lines.extend(
            (
                DIVIDER,
                f"已比较 {len(estimates)} 种配方，显示可估价材料成本最低的一种。",
            )
        )
    return _limit_reply(
        panel(
            "制作成本",
            lines,
            icon="🧮",
            subtitle=f"{item.name}　｜　{target}",
            footer="按当前最低挂单单价估算，不含税费；半成品按市场价，不递归拆解。",
        )
    )


async def _cooldown_remaining(event: Event) -> float:
    try:
        user_id = event.get_user_id()
    except Exception:
        user_id = "unknown"
    now = time.monotonic()
    async with _cooldown_lock:
        previous = _last_request_by_user.get(user_id, 0.0)
        remaining = MIN_USER_INTERVAL_SECONDS - (now - previous)
        if remaining <= 0:
            _last_request_by_user[user_id] = now
            return 0.0
        return remaining


async def _check_cooldown(matcher: type[Matcher], event: Event) -> None:
    remaining = await _cooldown_remaining(event)
    if remaining > 0:
        await matcher.finish(cooldown_panel(remaining))


def _usage_panel(title: str, message: str) -> str:
    return help_panel(title, message.splitlines())


async def _finish_error(matcher: type[Matcher], error: FFXIVToolsError) -> None:
    await matcher.finish(error_panel(_clean_text(error, 500)))


ff14 = on_command("ff14", force_whitespace=True, priority=10, block=True)
gather = on_command("gather", force_whitespace=True, priority=10, block=True)
sales = on_command("sales", force_whitespace=True, priority=10, block=True)
recip = on_command(
    "recip", aliases={"recipe"}, force_whitespace=True, priority=10, block=True
)
craftcost = on_command("craftcost", force_whitespace=True, priority=10, block=True)
cheapest = on_command("cheapest", force_whitespace=True, priority=10, block=True)


@ff14.handle()
async def handle_ff14(args: Message = CommandArg()) -> None:
    action = args.extract_plain_text().strip().lower()
    if action not in {"", "help"}:
        await ff14.finish(error_panel("/ff14 是插件查询目录。", hint="发送 /ff14 查看用法，再直接发送 /market、/dps 等具体查询命令。"))
    await ff14.finish(ff14_directory())


@gather.handle()
async def handle_gather(event: Event, args: Message = CommandArg()) -> None:
    query, usage = parse_item_command(args.extract_plain_text(), "gather")
    if usage:
        await gather.finish(_usage_panel("采集查询", usage))
    assert query is not None
    await _check_cooldown(gather, event)
    try:
        item = await resolve_item(query)
        sources = await fetch_gathering_sources(item)
        if not sources:
            raise FFXIVToolsError(f"“{item.name}”不是可直接采集的物品，或暂无采集点数据。")
        reply = format_gathering(item, sources)
    except FFXIVToolsError as exc:
        await _finish_error(gather, exc)
        return
    await gather.finish(reply)


@sales.handle()
async def handle_sales(event: Event, args: Message = CommandArg()) -> None:
    parsed, usage = parse_market_command(args.extract_plain_text(), "sales")
    if usage:
        await sales.finish(_usage_panel("成交查询", usage))
    assert parsed is not None
    query, target = parsed
    await _check_cooldown(sales, event)
    try:
        item = await resolve_item(query)
        payload = await fetch_sales(target, item.item_id)
        reply = format_sales(item, target, payload)
    except FFXIVToolsError as exc:
        await _finish_error(sales, exc)
        return
    await sales.finish(reply)


@recip.handle()
async def handle_recip(event: Event, args: Message = CommandArg()) -> None:
    query, usage = parse_item_command(args.extract_plain_text(), "recip")
    if usage:
        await recip.finish(_usage_panel("配方查询", usage + "\n别名：/recipe"))
    assert query is not None
    await _check_cooldown(recip, event)
    try:
        item = await resolve_item(query)
        recipes = await fetch_recipes(item)
        if not recipes:
            raise FFXIVToolsError(f"“{item.name}”没有可用的制作配方。")
        reply = format_recipes(item, recipes)
    except FFXIVToolsError as exc:
        await _finish_error(recip, exc)
        return
    await recip.finish(reply)


@craftcost.handle()
async def handle_craftcost(event: Event, args: Message = CommandArg()) -> None:
    parsed, usage = parse_market_command(args.extract_plain_text(), "craftcost")
    if usage:
        await craftcost.finish(_usage_panel("制作成本", usage))
    assert parsed is not None
    query, target = parsed
    await _check_cooldown(craftcost, event)
    try:
        item = await resolve_item(query)
        recipes = await fetch_recipes(item)
        if not recipes:
            raise FFXIVToolsError(f"“{item.name}”没有可用的制作配方。")
        item_ids = [
            item_id
            for recipe in recipes
            for item_id in (recipe.item_id, *(part.item_id for part in recipe.ingredients))
        ]
        market_payload = await fetch_current_market(target, item_ids, listings=50)
        markets = _market_item_map(market_payload)
        estimates = tuple(estimate_recipe(recipe, markets) for recipe in recipes)
        reply = format_craft_cost(item, target, estimates, markets)
    except FFXIVToolsError as exc:
        await _finish_error(craftcost, exc)
        return
    await craftcost.finish(reply)


@cheapest.handle()
async def handle_cheapest(event: Event, args: Message = CommandArg()) -> None:
    parsed, usage = parse_market_command(args.extract_plain_text(), "cheapest")
    if usage:
        await cheapest.finish(_usage_panel("跨服比价", usage))
    assert parsed is not None
    query, target = parsed
    await _check_cooldown(cheapest, event)
    try:
        item = await resolve_item(query)
        payload = await fetch_current_market(target, [item.item_id], listings=100)
        reply = format_cheapest(item, target, payload)
    except FFXIVToolsError as exc:
        await _finish_error(cheapest, exc)
        return
    await cheapest.finish(reply)


driver = get_driver()


@driver.on_startup
async def start_ffxiv_tools_client() -> None:
    await _get_http_client()
    logger.info("FFXIV data tools are ready (CN XIVAPI + Universalis)")


@driver.on_shutdown
async def close_ffxiv_tools_client() -> None:
    global _http_client
    if _http_client is not None and not _http_client.is_closed:
        await _http_client.aclose()
    _http_client = None
    _response_cache.clear()
    _last_request_by_user.clear()
