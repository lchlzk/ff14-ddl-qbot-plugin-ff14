import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import nonebot

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(driver="~fastapi+~httpx+~websockets")

from nonebot.adapters.qq import Bot
from nonebot.adapters.qq.config import BotInfo
from nonebot.adapters.qq.event import GroupAtMessageCreateEvent
from nonebot.message import handle_event
from bot_tools.storage import Store
from qbot_ff14 import commands


class CommandRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_commands_work_without_core_toolbox_matcher(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            adapter = MagicMock()
            adapter.config = nonebot.get_driver().config
            adapter.get_name.return_value = "QQ"
            bot = Bot(adapter, "test", BotInfo(id="test", token="", secret="test-only"))
            bot.send = AsyncMock()
            with patch.object(commands, "get_store", return_value=store), patch.object(store, "throttle"):
                for raw, expected in (("/fsx 暴击 3000", "副属性换算"),
                                      ("/ofish 靛青 1", "海钓"), ("/hunt", "手动狩猎时钟")):
                    with self.subTest(command=raw):
                        bot.send.reset_mock()
                        event = GroupAtMessageCreateEvent.model_validate({
                            "id": "fixture", "timestamp": "2026-09-04T00:00:00Z",
                            "content": raw, "to_me": True, "group_id": "fixture-group", "group_openid": "fixture-group",
                            "author": {"id": "member", "member_openid": "member", "member_role": "member", "bot": False},
                        })
                        await handle_event(bot, event)
                        self.assertEqual(bot.send.call_count, 1)
                        self.assertIn(expected, str(bot.send.call_args))
