import tempfile
import unittest
from unittest.mock import patch

from bot_tools import group_extensions
from bot_tools.storage import Identity, Store, ToolError
from bot_tools.web_admin import WebAdmin
from qbot_ff14.integration import register


class IntegrationTests(unittest.TestCase):
    def test_registered_admin_fields_search_and_persistence(self):
        with patch.dict(group_extensions._extensions, {}, clear=True), tempfile.TemporaryDirectory() as folder:
            register()
            store = Store(folder)
            who = Identity("test", "group:one", "owner", False, "owner")
            store.register(who)
            admin = WebAdmin(store)
            admin.update_group(who.scope_key, {"server": "梦羽宝境"})
            found = admin.groups(1, "梦羽宝境")
            self.assertEqual(found["total"], 1)
            row = found["items"][0]
            self.assertEqual(row["server"], "梦羽宝境")
            self.assertEqual(row["plugin_fields"][0]["name"], "server")
            self.assertEqual(row["plugin_fields"][0]["value"], "梦羽宝境")
            self.assertEqual(row["plugin_details"], [{"label": "狩猎规则", "value": "0 条"}])
            with self.assertRaises(ToolError):
                admin.update_group(who.scope_key, {"server": "超" * 31})
            reopened = WebAdmin(Store(folder))
            self.assertEqual(reopened.groups(1)["items"][0]["server"], "梦羽宝境")
            reopened.update_group(who.scope_key, {"server": ""})
            self.assertEqual(reopened.groups(1)["items"][0]["server"], "")
            self.assertEqual(reopened.groups(1, "梦羽宝境")["total"], 0)
