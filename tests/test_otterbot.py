from __future__ import annotations

import os
import json
import unittest
from unittest import mock

import httpx
import nonebot


os.environ.setdefault("OTTER_API_QQ", "123456789")
os.environ.setdefault("OTTER_API_TOKEN", "TESTTOKEN123")
os.environ.setdefault("OTTER_GLOBAL_MIN_INTERVAL", "0")
try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(driver="~fastapi+~httpx+~websockets")

from plugins import otterbot  # noqa: E402


class OtterFormattingTests(unittest.TestCase):
    def test_search_mapping(self) -> None:
        result = otterbot.format_search_result(
            {"title": "水晶", "content": "物品说明", "url": "https://example.invalid"},
            "水晶",
        )
        self.assertIn("🔎 物品搜索", result)
        self.assertIn("关键词　水晶", result)
        self.assertIn("水晶\n物品说明", result)

    def test_plain_result_removes_web_links_without_trusted_domain(self) -> None:
        result = otterbot.format_plain_result(
            "结果：https://example.invalid/detail 请在游戏内确认"
        )
        self.assertEqual(result, "结果： 请在游戏内确认")

    def test_links_can_be_opted_in_after_domain_approval(self) -> None:
        original_setting = otterbot.OTTER_INCLUDE_URLS
        try:
            otterbot.OTTER_INCLUDE_URLS = True
            result = otterbot.format_search_result(
                {
                    "title": "水晶",
                    "content": "物品说明",
                    "url": "https://example.invalid",
                },
                "水晶",
            )
            self.assertIn("水晶\n物品说明\nhttps://example.invalid", result)
        finally:
            otterbot.OTTER_INCLUDE_URLS = original_setting

    def test_reply_is_limited_by_utf8_bytes(self) -> None:
        result = otterbot.format_plain_result("中" * 2000)
        self.assertLessEqual(len(result.encode("utf-8")), otterbot.MAX_REPLY_BYTES)
        self.assertTrue(result.endswith("……内容过长"))

    def test_upstream_echo_of_token_is_redacted(self) -> None:
        result = otterbot.format_plain_result(
            f"unexpected credential: {otterbot.OTTER_API_TOKEN}"
        )
        self.assertNotIn(otterbot.OTTER_API_TOKEN, result)
        self.assertIn("凭据已隐藏", result)

    def test_search_removes_legacy_cq_image(self) -> None:
        result = otterbot.format_search_result(
            "结果[CQ:image,file=https://example.invalid/a.png]", "结果"
        )
        self.assertIn("🔎 物品搜索", result)
        self.assertTrue(result.endswith("结果"))
        self.assertNotIn("CQ:", result)

    def test_search_removes_large_legacy_cq_image_before_truncating(self) -> None:
        encoded_image = "A" * 200_000
        result = otterbot.format_search_result(
            f"[CQ:image,file=base64://{encoded_image}]最终结果", "结果"
        )
        self.assertTrue(result.endswith("最终结果"))
        self.assertNotIn("base64", result)

    def test_nested_quest_result(self) -> None:
        result = otterbot.format_quest_result(
            {"data": {"title": "任务", "content": "任务内容"}}, "任务"
        )
        self.assertIn("📜 任务查询", result)
        self.assertIn("任务\n任务内容", result)

    def test_weather_list_is_rendered_as_plain_text(self) -> None:
        result = otterbot.format_weather_result(
            [
                {
                    "pre_name": "晴朗",
                    "name": "暴雪",
                    "ET": "08:00",
                    "LT": "2026-09-03 12:00:00",
                }
            ],
            "优雷卡恒冰之地",
            "暴雪",
        )
        self.assertIn("晴朗→暴雪", result)
        self.assertIn("ET 08:00", result)

    def test_market_result_is_sorted_and_does_not_expose_retainer(self) -> None:
        result = otterbot.format_market_result(
            {
                "lastUploadTime": 1788443931186,
                "listings": [
                    {
                        "pricePerUnit": 120,
                        "quantity": 3,
                        "total": 360,
                        "hq": False,
                        "retainerName": "不应显示",
                    },
                    {
                        "pricePerUnit": 90,
                        "quantity": 2,
                        "total": 180,
                        "hq": True,
                        "retainerName": "也不应显示",
                    },
                ],
            },
            "火之水晶",
            "拂晓之间",
            False,
        )
        self.assertLess(result.index("90 金币/个"), result.index("120 金币/个"))
        self.assertIn("Universalis", result)
        self.assertNotIn("不应显示", result)

    def test_china_market_comparison_sorts_each_data_center_minimum(self) -> None:
        result = otterbot.format_china_market_comparison(
            {
                "陆行鸟": {
                    "listings": [
                        {"worldName": "红玉海", "pricePerUnit": 120, "quantity": 2},
                        {"worldName": "神意之地", "pricePerUnit": 100, "quantity": 3},
                    ]
                },
                "莫古力": {
                    "listings": [
                        {"worldName": "拂晓之间", "pricePerUnit": 80, "quantity": 4}
                    ]
                },
                "猫小胖": {"listings": []},
            },
            "火之水晶",
            False,
            ("豆豆柴",),
        )
        self.assertLess(result.index("莫古力"), result.index("陆行鸟"))
        self.assertIn("80 金币/个", result)
        self.assertIn("拂晓之间", result)
        self.assertIn("猫小胖　暂无符合条件的挂单", result)
        self.assertIn("豆豆柴　本次查询失败", result)

    def test_china_market_comparison_hq_filter_is_defensive(self) -> None:
        result = otterbot.format_china_market_comparison(
            {
                "陆行鸟": {
                    "listings": [
                        {"worldName": "红玉海", "pricePerUnit": 10, "quantity": 1, "hq": False},
                        {"worldName": "红玉海", "pricePerUnit": 20, "quantity": 2, "hq": True},
                    ]
                }
            },
            "巨匠药",
            True,
        )
        self.assertIn("20 金币/个", result)
        self.assertNotIn("10 金币/个", result)
        self.assertIn("仅 HQ", result)

class OtterArgumentTests(unittest.TestCase):
    def test_market_alias_shape(self) -> None:
        data, error = otterbot.parse_market_arguments("水晶 hq 萌芽池")
        self.assertIsNone(error)
        self.assertEqual(
            data,
            {"item_name": "水晶", "server_name": "萌芽池", "hq": True},
        )

    def test_market_accepts_china_comparison_aliases(self) -> None:
        for alias in ("国服", "全大区", "国服全大区", "中国", "China"):
            with self.subTest(alias=alias):
                data, error = otterbot.parse_market_arguments(f"火之水晶 {alias}")
                self.assertIsNone(error)
                self.assertEqual(
                    data,
                    {"item_name": "火之水晶", "server_name": "中国", "hq": False},
                )

    def test_china_data_centers_are_selected_without_traditional_cn_region(self) -> None:
        self.assertEqual(
            otterbot._extract_china_data_centers(
                [
                    {"name": "陆行鸟", "region": "中国"},
                    {"name": "莫古力", "region": "China"},
                    {"name": "陸行鳥", "region": "繁中服"},
                    {"name": "Elemental", "region": "Japan"},
                ]
            ),
            ("陆行鸟", "莫古力"),
        )

    def test_market_requires_item_and_server(self) -> None:
        data, error = otterbot.parse_market_arguments("水晶")
        self.assertIsNone(data)
        self.assertIn("参数不足", error or "")

    def test_market_does_not_treat_hq_inside_item_name_as_flag(self) -> None:
        data, error = otterbot.parse_market_arguments("HQcraft 萌芽池")
        self.assertIsNone(error)
        self.assertEqual(
            data,
            {"item_name": "HQcraft", "server_name": "萌芽池", "hq": False},
        )

    def test_market_accepts_hq_suffix_without_space(self) -> None:
        data, error = otterbot.parse_market_arguments("巨匠药HQ 萌芽池")
        self.assertIsNone(error)
        self.assertEqual(
            data,
            {"item_name": "巨匠药", "server_name": "萌芽池", "hq": True},
        )

    def test_market_resolves_cn_name_and_numeric_id(self) -> None:
        self.assertEqual(otterbot.resolve_market_item("火之水晶"), (8, "火之水晶"))
        self.assertEqual(otterbot.resolve_market_item("8"), (8, "火之水晶"))

    def test_house_rejects_extra_arguments(self) -> None:
        data, error = otterbot.parse_house_arguments("萌芽池 海雾村 小 多余")
        self.assertIsNone(data)
        self.assertIn("参数过多", error or "")

    def test_long_argument_is_rejected_locally(self) -> None:
        data, error = otterbot.parse_market_arguments("物" * 200)
        self.assertIsNone(data)
        self.assertIn("参数过长", error or "")

    def test_configuration_rejects_unicode_digits_as_qq(self) -> None:
        original_qq = otterbot.OTTER_API_QQ
        try:
            otterbot.OTTER_API_QQ = "１２３４５６７８"
            self.assertIn("5–20 位", otterbot._configuration_error() or "")
        finally:
            otterbot.OTTER_API_QQ = original_qq

    def test_luck_redraw(self) -> None:
        data, error = otterbot.parse_luck_arguments("redraw", "openid")
        self.assertIsNone(error)
        self.assertIsNotNone(data)
        assert data is not None
        self.assertNotEqual(data["user_id"], "openid")
        self.assertEqual(len(data["user_id"]), 32)
        self.assertTrue(data["redraw"])

    def test_luck_pseudonym_is_stable(self) -> None:
        first, _ = otterbot.parse_luck_arguments("", "openid")
        second, _ = otterbot.parse_luck_arguments("", "openid")
        different, _ = otterbot.parse_luck_arguments("", "another-openid")
        self.assertEqual(first, second)
        self.assertNotEqual(first, different)

    def test_house_optional_arguments(self) -> None:
        data, error = otterbot.parse_house_arguments("萌芽池 海雾村 小 部队")
        self.assertIsNone(error)
        self.assertEqual(
            data,
            {"server": "萌芽池", "area": "海雾村", "size": "小", "r_type": "部队"},
        )

    def test_weather_specific_default_count(self) -> None:
        data, error = otterbot.parse_weather_arguments("优雷卡恒冰之地 暴雪")
        self.assertIsNone(error)
        self.assertEqual(
            data,
            {"territory": "优雷卡恒冰之地", "weather": "暴雪", "length": 3},
        )

    def test_safe_dice_expression(self) -> None:
        with mock.patch.object(otterbot.secrets, "randbelow", side_effect=[0, 1, 2]):
            result, error = otterbot.roll_dice_expression("3d6+5")
        self.assertIsNone(error)
        self.assertEqual(result, "掷骰：3d6[1,2,3] +5\n结果：11")

    def test_dice_limits_number_of_dice(self) -> None:
        result, error = otterbot.roll_dice_expression("51d6")
        self.assertIsNone(result)
        self.assertIn("1–50", error or "")


class OtterApiClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        if otterbot._http_client is not None and not otterbot._http_client.is_closed:
            await otterbot._http_client.aclose()
        otterbot._http_client = None
        otterbot._market_cache.clear()
        otterbot._china_data_centers_cache = None

    async def test_universalis_market_does_not_send_otter_credentials(self) -> None:
        def handle_request(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.host, "universalis.app")
            self.assertTrue(request.url.path.endswith("/8"))
            self.assertEqual(request.url.params["listings"], "30")
            self.assertNotIn("token", request.url.params)
            self.assertNotIn("qq", request.url.params)
            return httpx.Response(
                200,
                json={"itemID": 8, "lastUploadTime": 0, "listings": []},
            )

        otterbot._http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handle_request)
        )
        result = await otterbot.call_universalis_market("拂晓之间", 8)
        self.assertEqual(result["itemID"], 8)

    async def test_universalis_hq_market_uses_server_side_filter(self) -> None:
        def handle_request(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.params["hq"], "true")
            return httpx.Response(
                200,
                json={"itemID": 8, "lastUploadTime": 0, "listings": []},
            )

        otterbot._http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handle_request)
        )
        await otterbot.call_universalis_market("陆行鸟", 8, True)

    async def test_china_market_comparison_queries_all_data_centers(self) -> None:
        async def fake_market(
            data_center: str, item_id: int, hq_only: bool = False
        ) -> dict[str, object]:
            self.assertEqual(item_id, 8)
            self.assertTrue(hq_only)
            return {"listings": [{"worldName": data_center, "pricePerUnit": 1, "quantity": 1}]}

        with (
            mock.patch.object(
                otterbot,
                "get_china_data_centers",
                mock.AsyncMock(return_value=("陆行鸟", "莫古力", "猫小胖", "豆豆柴")),
            ),
            mock.patch.object(otterbot, "call_universalis_market", side_effect=fake_market) as call,
        ):
            results, failed = await otterbot.call_china_market_comparison(8, True)

        self.assertEqual(set(results), {"陆行鸟", "莫古力", "猫小胖", "豆豆柴"})
        self.assertEqual(failed, ())
        self.assertEqual(call.await_count, 4)

    async def test_universalis_unknown_world_has_friendly_error(self) -> None:
        otterbot._http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(404, json={"error": "unknown"})
            )
        )
        with self.assertRaisesRegex(otterbot.OtterApiError, "服务器或物品"):
            await otterbot.call_universalis_market("不存在", 8)

    async def test_request_uses_expected_query_and_json(self) -> None:
        def handle_request(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.params["tracker"], "webapi")
            self.assertEqual(request.url.params["qq"], "123456789")
            self.assertEqual(request.url.params["token"], "TESTTOKEN123")
            self.assertEqual(
                json.loads(request.content),
                {"request": "search", "data": {"name": "水晶"}},
            )
            return httpx.Response(
                200,
                json={"response": "success", "rcode": "0", "msg": "", "data": "结果"},
            )

        otterbot._http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handle_request)
        )
        result = await otterbot.call_otter_api("search", {"name": "水晶"})
        self.assertEqual(result, "结果")

    async def test_privacy_unsafe_botlist_is_not_allowed(self) -> None:
        with self.assertRaisesRegex(otterbot.OtterApiError, "未启用"):
            await otterbot.call_otter_api("botlist", {})

    async def test_invalid_api_token_has_safe_error(self) -> None:
        def handle_request(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "response": "error",
                    "rcode": "101",
                    "msg": "Invalid API token",
                },
            )

        otterbot._http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handle_request)
        )
        with self.assertRaisesRegex(otterbot.OtterApiError, "Token 无效") as context:
            await otterbot.call_otter_api("search", {"name": "水晶"})
        self.assertNotIn("TESTTOKEN123", str(context.exception))

    async def test_quest_not_found_has_friendly_error(self) -> None:
        def handle_request(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "response": "error",
                    "rcode": "1041",
                    "msg": "Quest not found https://example.invalid",
                },
            )

        otterbot._http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handle_request)
        )
        with self.assertRaisesRegex(otterbot.OtterApiError, "没有找到") as context:
            await otterbot.call_otter_api("quest", {"name": "不存在"})
        self.assertNotIn("http", str(context.exception))

    async def test_unknown_upstream_error_does_not_echo_details(self) -> None:
        def handle_request(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "response": "error",
                    "rcode": "9999",
                    "msg": "internal path and sensitive diagnostic",
                },
            )

        otterbot._http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handle_request)
        )
        with self.assertRaisesRegex(otterbot.OtterApiError, "9999") as context:
            await otterbot.call_otter_api("search", {"name": "水晶"})
        self.assertNotIn("sensitive", str(context.exception))

    async def test_declared_oversized_response_is_rejected(self) -> None:
        def handle_request(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={
                    "content-length": str(otterbot.OTTER_MAX_RESPONSE_BYTES + 1)
                },
                content=b"{}",
            )

        otterbot._http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handle_request)
        )
        with self.assertRaisesRegex(otterbot.OtterApiError, "内容过大"):
            await otterbot.call_otter_api("search", {"name": "水晶"})


if __name__ == "__main__":
    unittest.main()
