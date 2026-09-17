"""Group-scoped manual hunt timers; data keys remain compatible."""
import time
from message_ui import help_panel, panel
from bot_tools.community import integer, stamp
from bot_tools.storage import Identity, Store, ToolError, clean

def hunt(store: Store, who: Identity, raw: str) -> str:
    args = raw.split()
    if not args:
        return help_panel("手动狩猎时钟", ["管理员设置规则：/hunt rule 怪物 最早小时 最晚小时", "示例：/hunt rule 测试怪 4 6", "/hunt kill 怪物 [服务器] [已过去的分钟]", "/hunt check 怪物 [服务器]", "/hunt list [服务器]", "/hunt undo 怪物 [服务器]", "/group server 梦羽宝境"], footer="窗口由管理员设置，不包含自动通报、触发条件或维护重置；过窗不等于已刷新。")
    with store.state(who.scope_key) as doc:
        rules = doc.setdefault("hunt_rules", {})
        kills = doc.setdefault("hunt_kills", {})
        op = args[0]
        if op == "rule":
            store.require_admin(who)
            if len(args) != 4:
                raise ToolError("用法：/hunt rule 怪物 最早小时 最晚小时")
            name = clean(args[1], 40)
            try:
                early, late = float(args[2]), float(args[3])
            except ValueError:
                raise ToolError("小时必须是数字。") from None
            if not (0 < early <= late <= 720):
                raise ToolError("需要满足 0 < 最早 ≤ 最晚 ≤ 720 小时。")
            if name not in rules and len(rules) >= 100:
                raise ToolError("规则已满（最多 100 个）。")
            rules[name] = [early, late]
            return panel("狩猎规则已保存", f"{name} · 击杀后 {early:g}～{late:g} 小时窗口", footer="这是管理员手动设定的范围，不是官方刷新保证。")
        if op == "list":
            if len(args) > 2:
                raise ToolError("用法：/hunt list [服务器]")
            server = args[1] if len(args) == 2 else doc.get("server", "")
            rows = [v for v in kills.values() if v["server"] == server and v["times"]]
            return panel("狩猎记录 · "+server, [f"{v['name']} · 最近击杀 {stamp(v['times'][-1])}" for v in rows[:12]] or ["没有击杀记录；先设置默认服务器和狩猎规则。"], footer="/hunt check 怪物 服务器 查看窗口。")
        if op not in {"kill", "check", "undo"} or not 2 <= len(args) <= (4 if op == "kill" else 3):
            raise ToolError("发送 /hunt 查看用法。")
        name = clean(args[1], 40)
        server = args[2] if len(args) >= 3 else doc.get("server", "")
        if not server:
            raise ToolError("请提供服务器，或先 /group server 服务器。")
        server = clean(server, 30)
        if name not in rules:
            raise ToolError("尚未设置此怪物的窗口规则。管理员请先 /hunt rule 怪物 最早小时 最晚小时")
        key = name + "|" + server
        if key not in kills and len(kills) >= 500:
            raise ToolError("此会话狩猎记录已满。")
        record = kills.setdefault(key, {"name":name,"server":server,"times":[]})
        if op in {"kill", "undo"}:
            store.require_admin(who)
            if op == "kill":
                minutes = integer(args[3], 0, 43200) if len(args) == 4 else 0
                record["times"].append(time.time()-minutes*60)
                record["times"] = record["times"][-20:]
            elif record["times"]:
                record["times"].pop()
                return panel("已撤销上一条击杀记录", f"{name} · {server}", footer="可查询上一条记录；最多保留最近 20 次。")
        if not record["times"]:
            raise ToolError("尚无击杀记录。")
        last = record["times"][-1]
        early, late = [last+h*3600 for h in rules[name]]
        status = "窗口未到" if time.time()<early else ("窗口内" if time.time()<=late else "已过窗口上限；需确认是否漏记")
        return panel(f"{name} · {server}", [status, f"最后击杀：{stamp(last)}", f"估算窗口：{stamp(early)} ～ {stamp(late)}"], footer="北京时间；仅根据本群手动记录估算，不代表怪物已出现。")
