"""FF14-only calculations and persistent hunt behavior."""
import tempfile
import unittest
from bot_tools import community as c
from bot_tools.storage import Identity, Store, ToolError
from qbot_ff14 import games
from qbot_ff14.hunt import hunt
from qbot_ff14.integration import register

register()

class GameTests(unittest.TestCase):
    def test_stats_base_and_breakpoints(self):
        self.assertIn("5.0%",games.stat_values("crit",420)[0])
        self.assertIn("1.000",games.stat_values("det",440)[0])
        self.assertEqual(games.stat_values("speed",441)[1],250)
        self.assertEqual(games.stat_values("speed",442)[1],249)
        self.assertIn("+22",games.fsx("技速 420"))
        self.assertIn("1.103",games.stat_values("ten",3000)[0])
        self.assertEqual(games.stat_values("pie",458)[1],0)
        self.assertEqual(games.stat_values("pie",459)[1],1)

    def test_stats_validation(self):
        for arg in ["暴击 1","det=420","crit=nan","speed=99999","职业 1000"]:
            with self.assertRaises(ToolError,msg=arg):
                games.fsx(arg)
        self.assertIn("副属性换算",games.fsx("crit=3000 dh=1600 det=2200"))

    def test_ocean_epoch_and_rotation(self):
        self.assertEqual(games.route_at(0),"RN") # (88 % 12 + 88 // 12) = 11
        self.assertEqual(games.route_at(7200*144),games.route_at(0))
        self.assertEqual(games.route_at(0,"ruby74"),"RN")
        self.assertEqual(games.route_at(7200*72,"ruby74"),games.route_at(0,"ruby74"))

    def test_ocean_registration_boundary(self):
        before=games.ofish("1",899)
        after=games.ofish("1",900)
        self.assertIn("08:00",before)
        self.assertIn("正在报名",before)
        self.assertIn("10:00",after)
        self.assertNotIn("正在报名",after)
        self.assertIn("红玉 / 7.5 起",games.ofish("红玉新 1",0))
        for arg in ("红玉 3","0","6"):
            with self.assertRaises(ToolError): games.ofish(arg)


class HuntTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.owner = Identity("app","group:a","owner")
        self.member = Identity("app","group:a","member")
        self.other = Identity("app","group:b","member")
        self.private = Identity("app","private:owner","owner",True)
        for who in (self.owner,self.member,self.other,self.private):
            self.store.register(who)
        # A pre-upgrade group grant remains supported, but new superadmin
        # grants must use the private two-step workflow.
        with self.store.connect() as db:
            db.execute("UPDATE identities SET role='owner' WHERE actor=?", (self.owner.actor,))
        record = self.store.redeem_admin_token(self.private, self.store.create_admin_token())
        self.store.grant(record["code"], "owner")

    def test_hunt_rules_and_undo(self):
        with self.assertRaises(ToolError):
            hunt(self.store,self.member,"rule 测试怪 4 6")
        with self.assertRaises(ToolError):
            hunt(self.store,self.owner,"rule 测试怪 nan 6")
        hunt(self.store,self.owner,"rule 测试怪 4 6")
        c.management(self.store,self.owner,"group","server 梦羽宝境")
        hunt(self.store,self.owner,"kill 测试怪")
        with self.assertRaises(ToolError):
            hunt(self.store,self.member,"kill 测试怪")
        result=hunt(self.store,self.member,"check 测试怪")
        self.assertIn("窗口未到",result)
        hunt(self.store,self.owner,"undo 测试怪")
        with self.assertRaises(ToolError):
            hunt(self.store,self.owner,"check 测试怪")
