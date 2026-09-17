from __future__ import annotations

import base64
import json
import os
import unittest

import httpx
import nonebot


os.environ.setdefault("FFLOGS_CLIENT_ID", "TEST_CLIENT_ID")
os.environ.setdefault("FFLOGS_CLIENT_SECRET", "TEST_CLIENT_SECRET")
os.environ.setdefault("FFLOGS_GLOBAL_INTERVAL", "0")
os.environ.setdefault("FFLOGS_CACHE_TTL", "300")
try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(driver="~fastapi+~httpx+~websockets")

from plugins import fflogs  # noqa: E402


def sample_character() -> dict:
    return {
        "id": 42,
        "name": "测试角色",
        "hidden": False,
        "server": {
            "name": "萌芽池",
            "slug": "萌芽池",
            "region": {"compactName": "CN", "name": "China"},
        },
        "zoneRankings": {
            "zone": 73,
            "metric": "rdps",
            "bestPerformanceAverage": 91.25,
            "medianPerformanceAverage": 82.5,
            "rankings": [
                {
                    "encounter": {"id": 101, "name": "测试首领一"},
                    "rankPercent": 95.2,
                    "medianPercent": 80.0,
                    "bestAmount": 12345.6,
                    "totalKills": 3,
                    "fastestKill": 125000,
                    "bestSpec": "Dragoon",
                },
                {
                    "encounter": {"id": 102, "name": "测试首领二"},
                    "rankPercent": 0,
                    "medianPercent": 0,
                    "bestAmount": 0,
                    "totalKills": 0,
                    "bestSpec": "Dragoon",
                },
            ],
            "allStars": [
                {
                    "spec": "Dragoon",
                    "rankPercent": 96.5,
                    "points": 583.2,
                    "rank": 60,
                }
            ],
        },
    }


class FFLogsArgumentTests(unittest.TestCase):
    def test_cn_character_and_modifiers(self) -> None:
        parsed, error = fflogs.parse_character_arguments(
            "测试角色 萌芽池 region=CN zone=73 job=龙骑 metric=cdps", "dps"
        )
        self.assertIsNone(error)
        self.assertEqual(
            parsed,
            fflogs.CharacterQuery(
                "测试角色", "萌芽池", "CN", 73, "Dragoon", "cdps"
            ),
        )

    def test_pipe_syntax_preserves_english_name(self) -> None:
        parsed, error = fflogs.parse_character_arguments(
            "North Face | Chocobo | JP | 76 | RDM | ndps", "raid"
        )
        self.assertIsNone(error)
        self.assertEqual(
            parsed,
            fflogs.CharacterQuery(
                "North Face", "Chocobo", "JP", 76, "RedMage", "ndps"
            ),
        )

    def test_bad_zone_is_rejected(self) -> None:
        parsed, error = fflogs.parse_character_arguments(
            "测试角色 萌芽池 zone=abc", "raid"
        )
        self.assertIsNone(parsed)
        self.assertIn("正整数", error or "")

    def test_unknown_metric_is_rejected(self) -> None:
        parsed, error = fflogs.parse_character_arguments(
            "测试角色 萌芽池 metric=hps", "dps"
        )
        self.assertIsNone(parsed)
        self.assertIn("rdps", error or "")

    def test_legacy_arbitrary_score_syntax_gets_migration_help(self) -> None:
        parsed, error = fflogs.parse_character_arguments(
            "绝亚历山大 骑士 12500 国服 day#7", "dps"
        )
        self.assertIsNone(parsed)
        self.assertIn("数据源已停用", error or "")
        self.assertIn("角色名", error or "")

    def test_cn_uses_cn_host_and_global_region_uses_global_host(self) -> None:
        self.assertEqual(fflogs._host_for_region("CN"), "https://cn.fflogs.com")
        self.assertEqual(fflogs._host_for_region("JP"), "https://www.fflogs.com")

    def test_floor_alias_selects_zone_and_specific_floor(self) -> None:
        parsed, error = fflogs.parse_character_arguments(
            "测试角色 萌芽池 副本=E8S", "dps"
        )
        self.assertIsNone(error)
        self.assertEqual(
            parsed,
            fflogs.CharacterQuery(
                "测试角色", "萌芽池", "CN", 33, None, "rdps", "E8S"
            ),
        )

    def test_bare_floor_alias_is_accepted(self) -> None:
        parsed, error = fflogs.parse_character_arguments(
            "测试角色 萌芽池 M9S", "dps"
        )
        self.assertIsNone(error)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual((parsed.zone_id, parsed.floor), (73, "M9S"))


class FFLogsFormattingTests(unittest.TestCase):
    def test_dps_result_uses_actual_zone_rankings_fields(self) -> None:
        query = fflogs.CharacterQuery(
            "测试角色", "萌芽池", "CN", 73, "Dragoon", "rdps"
        )
        result = fflogs.format_dps_result(sample_character(), query)
        self.assertIn("FF Logs · rDPS 历史排名", result)
        self.assertIn("M9S · 测试首领一", result)
        self.assertIn("12,345.6 rDPS", result)
        self.assertIn("历史最佳 95%", result)
        self.assertIn("公开击杀 3", result)
        self.assertIn("最快 2:05", result)
        self.assertIn("公开上传", result)

    def test_raid_result_never_claims_no_clear(self) -> None:
        query = fflogs.CharacterQuery("测试角色", "萌芽池", "CN", 73)
        result = fflogs.format_raid_result(sample_character(), query)
        self.assertIn("公开击杀 3 次", result)
        self.assertIn("暂无公开排名", result)
        self.assertIn("不代表未通关", result)
        self.assertNotIn("仍未攻破", result)

    def test_zero_values_are_not_rendered_as_fake_dps(self) -> None:
        query = fflogs.CharacterQuery("测试角色", "萌芽池", "CN", 73)
        result = fflogs.format_dps_result(sample_character(), query)
        empty_line = next(line for line in result.splitlines() if "M10S" in line)
        self.assertIn("暂无公开排名", empty_line)
        self.assertNotIn("0.0", empty_line)
        self.assertNotIn("未知职业", result)

    def test_historical_percent_and_duration_match_character_page_display(self) -> None:
        character = sample_character()
        ranking = character["zoneRankings"]["rankings"][0]
        ranking["rankPercent"] = 89.9624
        ranking["medianPercent"] = 45.8769
        ranking["bestAmount"] = 17381.571500572
        ranking["fastestKill"] = 660526
        result = fflogs.format_dps_result(
            character,
            fflogs.CharacterQuery("测试角色", "萌芽池", "CN", 73),
        )
        self.assertIn("历史最佳 89%", result)
        self.assertIn("历史中位 45%", result)
        self.assertIn("17,381.6 rDPS", result)
        self.assertIn("最快 11:00", result)

    def test_e8s_filters_to_shiva(self) -> None:
        character = sample_character()
        character["zoneRankings"] = {
            "zone": 33,
            "rankings": [
                {"encounter": {"id": 69, "name": "拉姆"}, "bestAmount": 100},
                {"encounter": {"id": 72, "name": "希瓦"}, "bestAmount": 200},
            ],
        }
        query = fflogs.CharacterQuery(
            "测试角色", "萌芽池", "CN", 33, None, "rdps", "E8S"
        )
        result = fflogs.format_dps_result(character, query)
        self.assertIn("E8S · 希瓦", result)
        self.assertNotIn("拉姆", result)

    def test_zone_catalog_shows_short_name_and_id(self) -> None:
        result = fflogs.format_zone_catalog(
            (
                {
                    "id": 73,
                    "name": "阿卡狄亚登天斗技场 重量级",
                    "frozen": False,
                    "encounters": ("致命美人",),
                },
            ),
            "CN",
        )
        self.assertIn("73 · M9S–M12S", result)
        self.assertIn("排名分区 ID", result)

    def test_hidden_character_has_clear_error(self) -> None:
        character = sample_character()
        character["hidden"] = True
        with self.assertRaisesRegex(fflogs.FFLogsError, "隐藏"):
            fflogs.format_raid_result(
                character,
                fflogs.CharacterQuery("测试角色", "萌芽池", "CN"),
            )


class FFLogsApiClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fflogs._access_tokens.clear()
        fflogs._response_cache.clear()

    async def asyncTearDown(self) -> None:
        if fflogs._http_client is not None and not fflogs._http_client.is_closed:
            await fflogs._http_client.aclose()
        fflogs._http_client = None
        fflogs._access_tokens.clear()
        fflogs._response_cache.clear()

    async def test_cn_oauth_graphql_and_token_cache(self) -> None:
        requests: list[httpx.Request] = []

        def handle_request(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path == "/oauth/token":
                expected = base64.b64encode(
                    b"TEST_CLIENT_ID:TEST_CLIENT_SECRET"
                ).decode("ascii")
                self.assertEqual(request.headers["Authorization"], f"Basic {expected}")
                self.assertEqual(request.content, b"grant_type=client_credentials")
                return httpx.Response(
                    200, json={"access_token": "SAFE_TOKEN", "expires_in": 3600}
                )

            self.assertEqual(request.url.path, "/api/v2/client")
            self.assertEqual(request.headers["Authorization"], "Bearer SAFE_TOKEN")
            payload = json.loads(request.content)
            self.assertIn("timeframe: Historical", payload["query"])
            self.assertNotIn("timeframe: Today", payload["query"])
            self.assertEqual(payload["variables"]["serverRegion"], "CN")
            self.assertEqual(payload["variables"]["zoneID"], 73)
            self.assertEqual(payload["variables"]["metric"], "rdps")
            return httpx.Response(
                200,
                json={
                    "data": {"characterData": {"character": sample_character()}}
                },
            )

        fflogs._http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handle_request)
        )
        query = fflogs.CharacterQuery("测试角色", "萌芽池", "CN", 73)
        first = await fflogs.query_character_rankings(query)
        second = await fflogs.query_character_rankings(query)
        self.assertEqual(first["name"], "测试角色")
        self.assertIs(first, second)
        self.assertEqual([request.url.host for request in requests], [
            "cn.fflogs.com",
            "cn.fflogs.com",
        ])

    async def test_graphql_error_does_not_echo_upstream_details(self) -> None:
        def handle_request(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth/token":
                return httpx.Response(
                    200, json={"access_token": "SAFE_TOKEN", "expires_in": 3600}
                )
            return httpx.Response(
                200,
                json={
                    "errors": [
                        {"message": "database path and internal secret diagnostic"}
                    ]
                },
            )

        fflogs._http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handle_request)
        )
        with self.assertRaisesRegex(fflogs.FFLogsError, "无法完成") as context:
            await fflogs.query_character_rankings(
                fflogs.CharacterQuery("不存在", "萌芽池", "CN", 73)
            )
        self.assertNotIn("internal", str(context.exception))

    async def test_declared_oversized_response_is_rejected(self) -> None:
        def handle_request(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-length": str(fflogs.MAX_RESPONSE_BYTES + 1)},
                content=b"{}",
            )

        fflogs._http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handle_request)
        )
        with self.assertRaisesRegex(fflogs.FFLogsError, "内容过大"):
            await fflogs.query_character_rankings(
                fflogs.CharacterQuery("测试角色", "萌芽池", "CN", 73)
            )


if __name__ == "__main__":
    unittest.main()
