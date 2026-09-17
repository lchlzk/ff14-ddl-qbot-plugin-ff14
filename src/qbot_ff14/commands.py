"""Native QQ entry points for the local FF14 tools."""
import asyncio

from nonebot import on_command
from nonebot.adapters.qq import Bot, Message
from nonebot.adapters.qq.event import MessageEvent
from nonebot.params import Command, CommandArg
from nonebot.log import logger

from bot_tools import community
from bot_tools.storage import ToolError
from bot_tools.plugin_runtime import bounded, command_eligible, get_store, identity
from message_ui import error_panel, public_error_message
from . import games
from .hunt import hunt


utilities = on_command("fsx", aliases={"ofish", "hunt"}, force_whitespace=True,
                       rule=command_eligible, priority=10, block=True)


async def dispatch(bot: Bot, event: MessageEvent, name: str, raw: str) -> str:
    store, who = get_store(), identity(bot, event)
    is_help = raw == "help" or (not raw and name != "ofish")
    await asyncio.to_thread(community.gate, store, who, name)
    if not is_help:
        await asyncio.to_thread(store.throttle, who.actor)
        await asyncio.to_thread(community.gate, store, who, name, True)
    if name == "hunt":
        return await asyncio.to_thread(hunt, store, who, "" if raw == "help" else raw)
    if name in {"fsx", "ofish"}:
        return await asyncio.to_thread(getattr(games, name), raw)
    raise ToolError("未知 FF14 工具命令。")


@utilities.handle()
async def handle_utilities(bot: Bot, event: MessageEvent,
                           command: tuple[str, ...] = Command(), args: Message = CommandArg()) -> None:
    try:
        reply = await dispatch(bot, event, command[0], args.extract_plain_text().strip())
    except ToolError as exc:
        reply = error_panel(public_error_message(exc, "FF14 工具暂时不可用，请稍后重试。"))
    except Exception as exc:
        logger.error("FF14 local tool failed ({})", type(exc).__name__)
        reply = error_panel("FF14 工具暂时不可用，请稍后重试。")
    await utilities.finish(bounded(reply))
