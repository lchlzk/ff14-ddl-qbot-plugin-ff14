from __future__ import annotations

import os
import unittest

import httpx
import nonebot


os.environ.setdefault("OTTER_GLOBAL_MIN_INTERVAL", "0")
try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(driver="~fastapi+~httpx+~websockets")

from plugins import ffxiv_tools as tools  # noqa: E402


def relation(row_id: int, name: str) -> dict[str, object]:
    return {"row_id": row_id, "fields": {"Name": name}}


class ArgumentTests(unittest.TestCase):
    def test_market_command_keeps_spaces_in_item_name(self) -> None:
        parsed, error = tools.parse_market_command("高级 巧力之幻药 陆行鸟", "sales")
        self.assertIsNone(error)
        self.assertEqual(parsed, ("高级 巧力之幻药", "陆行鸟"))

    def test_market_command_requires_target(self) -> None:
        parsed, error = tools.parse_market_command("火之水晶", "cheapest")
        self.assertIsNone(parsed)
        self.assertIn("参数不足", error or "")

    def test_item_query_rejects_filter_control_characters(self) -> None:
        parsed, error = tools.parse_item_command('Name="火之水晶"', "gather")
        self.assertIsNone(parsed)
        self.assertIn("引号", error or "")


class RecipeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.item = tools.ItemInfo(5361, "枫木木材")
        self.recipe_payload = {
            "row_id": 1008,
            "fields": {
                "AmountIngredient": [3, 0, 1],
                "AmountResult": 1,
                "CraftType": relation(0, "木工"),
                "Ingredient": [
                    relation(5380, "枫木原木"),
                    relation(0, ""),
                    relation(4, "风之碎晶"),
                ],
                "ItemResult": relation(5361, "枫木木材"),
                "RecipeLevelTable": {
                    "fields": {"ClassJobLevel": 1, "Stars": 0}
                },
            },
        }

    def test_recipe_parser_pairs_materials_and_amounts(self) -> None:
        recipe = tools._parse_recipe(self.recipe_payload, self.item)
        self.assertIsNotNone(recipe)
        assert recipe is not None
        self.assertEqual(recipe.craft_type, "木工")
        self.assertEqual(recipe.level, 1)
        self.assertEqual(
            recipe.ingredients,
            (
                tools.Ingredient(5380, "枫木原木", 3),
                tools.Ingredient(4, "风之碎晶", 1),
            ),
        )

    def test_recipe_output_is_qq_friendly(self) -> None:
        recipe = tools._parse_recipe(self.recipe_payload, self.item)
        assert recipe is not None
        rendered = tools.format_recipes(self.item, [recipe])
        self.assertIn("📖 制作配方", rendered)
        self.assertIn("木工 Lv.1", rendered)
        self.assertIn("枫木原木 ×3", rendered)


class GatheringTests(unittest.TestCase):
    def test_gathering_output_contains_method_level_and_place(self) -> None:
        rendered = tools.format_gathering(
            tools.ItemInfo(5380, "枫木原木"),
            [
                tools.GatheringSource(
                    11,
                    "采伐",
                    5,
                    3,
                    False,
                    ("黑衣森林中央林区 · 翡翠湖滨",),
                )
            ],
        )
        self.assertIn("⛏️ 采集地点", rendered)
        self.assertIn("采伐 Lv.5", rendered)
        self.assertIn("黑衣森林中央林区 · 翡翠湖滨", rendered)


class SalesTests(unittest.TestCase):
    def test_sales_output_has_weighted_average_and_hides_buyer(self) -> None:
        rendered = tools.format_sales(
            tools.ItemInfo(8, "火之水晶"),
            "梦羽宝境",
            {
                "entries": [
                    {
                        "pricePerUnit": 100,
                        "quantity": 1,
                        "timestamp": 1788357861,
                        "buyerName": "不应显示",
                        "hq": False,
                    },
                    {
                        "pricePerUnit": 200,
                        "quantity": 3,
                        "timestamp": 1788269683,
                        "buyerName": "仍不应显示",
                        "hq": True,
                    },
                ],
                "regularSaleVelocity": 12.5,
            },
        )
        self.assertIn("样本均价　175 金币/个", rendered)
        self.assertIn("参考售速　约 12.5 个/天", rendered)
        self.assertNotIn("不应显示", rendered)


class MarketTests(unittest.TestCase):
    def test_cheapest_groups_each_world_at_its_lowest_price(self) -> None:
        rendered = tools.format_cheapest(
            tools.ItemInfo(8, "火之水晶"),
            "陆行鸟",
            {
                "itemID": 8,
                "listings": [
                    {"worldName": "红玉海", "pricePerUnit": 44, "quantity": 10},
                    {"worldName": "红玉海", "pricePerUnit": 50, "quantity": 99},
                    {"worldName": "晨曦王座", "pricePerUnit": 45, "quantity": 20},
                ],
            },
        )
        self.assertLess(rendered.index("红玉海"), rendered.index("晨曦王座"))
        self.assertIn("44/个", rendered)
        self.assertNotIn("50/个", rendered)

    def test_market_item_map_accepts_multi_item_response(self) -> None:
        result = tools._market_item_map(
            {"items": {"8": {"itemID": 8}, "4": {"itemID": 4}}}
        )
        self.assertEqual(set(result), {4, 8})

    def test_recipe_estimate_uses_lowest_unit_prices(self) -> None:
        recipe = tools.RecipeInfo(
            1008,
            5361,
            "枫木木材",
            1,
            "木工",
            1,
            0,
            (
                tools.Ingredient(5380, "枫木原木", 3),
                tools.Ingredient(4, "风之碎晶", 1),
            ),
        )
        estimate = tools.estimate_recipe(
            recipe,
            {
                5380: {
                    "listings": [
                        {"pricePerUnit": 20, "quantity": 10},
                        {"pricePerUnit": 10, "quantity": 1},
                    ]
                },
                4: {"listings": [{"pricePerUnit": 5, "quantity": 10}]},
            },
        )
        self.assertEqual(estimate.total, 35)
        self.assertEqual(estimate.missing, ())

    def test_craft_cost_does_not_claim_complete_total_when_market_is_missing(self) -> None:
        recipe = tools.RecipeInfo(
            1008,
            5361,
            "枫木木材",
            1,
            "木工",
            1,
            0,
            (tools.Ingredient(5380, "枫木原木", 3),),
        )
        estimate = tools.estimate_recipe(recipe, {})
        rendered = tools.format_craft_cost(
            tools.ItemInfo(5361, "枫木木材"), "陆行鸟", [estimate], {}
        )
        self.assertIn("无法计算完整成本", rendered)
        self.assertNotIn("每炉材料成本", rendered)

    def test_reply_limit_is_measured_in_utf8_bytes(self) -> None:
        rendered = tools._limit_reply("中" * 2000)
        self.assertLessEqual(len(rendered.encode("utf-8")), tools.MAX_REPLY_BYTES)
        self.assertTrue(rendered.endswith("……内容过长"))


class ResolveItemTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        tools._response_cache.clear()

    async def asyncTearDown(self) -> None:
        if tools._http_client is not None:
            await tools._http_client.aclose()
        tools._http_client = None
        tools._response_cache.clear()

    async def test_numeric_item_id_uses_chinese_sheet(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/api/sheet/Item/8")
            self.assertEqual(request.url.params["language"], "chs")
            return httpx.Response(200, json={"row_id": 8, "fields": {"Name": "火之水晶"}})

        tools._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        item = await tools.resolve_item("8")
        self.assertEqual(item, tools.ItemInfo(8, "火之水晶"))


if __name__ == "__main__":
    unittest.main()
