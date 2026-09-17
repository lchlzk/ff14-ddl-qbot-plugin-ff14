"""Local FF14 stat calculations and ocean-fishing timetable."""
import time
from message_ui import help_panel, panel
from bot_tools.community import integer, stamp
from bot_tools.storage import ToolError

STAT_NAMES = {"暴击":"crit","直击":"dh","信念":"det","坚韧":"ten","信仰":"pie","技速":"speed","咏速":"speed","速度":"speed","sks":"speed","sps":"speed"}


def stat_values(stat: str, value: int) -> tuple[str, int]:
    """7.x, level 100, no food changes/haste/job traits. Integer game rounding."""
    sub, main, div = 420, 440, 2780
    if stat == "crit":
        tier = 200*(value-sub)//div
        return f"暴击率 {(tier+50)/10:.1f}% · 暴击倍率 {(tier+1400)/1000:.3f}", tier
    if stat == "dh":
        tier = 550*(value-sub)//div
        return f"直击率 {min(1000, max(0,tier))/10:.1f}% · 普通直击倍率 1.250", tier
    if stat == "det":
        tier = 140*(value-main)//div
        return f"信念伤害/治疗倍率 {(1000+tier)/1000:.3f}", tier
    if stat == "ten":
        tier = 112*(value-sub)//div
        reduction = 200*(value-sub)//div
        return f"坚韧输出倍率 {(1000+tier)/1000:.3f} · 减伤 {reduction/10:.1f}%（坦克）", (tier*10000+reduction)
    if stat == "pie":
        tier = 150*(value-main)//div
        return f"每次自然回蓝额外 +{tier} MP（仅治疗职业）", tier
    if stat == "speed":
        tier = 130*(value-sub)//div
        gcd_centis = 2500*(1000-tier)//10000
        return f"2.50s 基础 GCD → {gcd_centis/100:.2f}s\n持续伤害/治疗系数 {(1000+tier)/1000:.3f}", gcd_centis
    raise ToolError("支持：暴击、直击、信念、坚韧、信仰、技速、咏速。")


def fsx(raw: str) -> str:
    args = raw.split()
    if not args or args == ["help"]:
        return help_panel("副属性计算 · Lv.100", ["/fsx 暴击 3000", "/fsx 技速 1200", "/fsx crit=3000 dh=1600 det=2200 speed=420", "支持：暴击/crit、直击/dh、信念/det、坚韧/ten、信仰/pie、技速/咏速/speed"], footer="7.x / 100 级公式；不自动读取角色装备，不包含急速和职业特性。")
    if len(args) == 2 and "=" not in args[0]:
        pairs = [(args[0],args[1])]
    else:
        if not 1 <= len(args) <= 6 or any(a.count("=") != 1 for a in args):
            raise ToolError("用法：/fsx 暴击 3000，或 /fsx crit=3000 dh=1600")
        pairs = [tuple(a.split("=")) for a in args]
    lines = []
    for key, number in pairs:
        stat = STAT_NAMES.get(key, key.lower())
        value = integer(number, 440 if stat in {"det","pie"} else 420, 6500)
        result, tier = stat_values(stat, value)
        next_value = next((v for v in range(value+1, 7001) if stat_values(stat,v)[1] != tier), None)
        lines.extend([f"{key} {value}", result, f"下一{'GCD' if stat=='speed' else '数值'}档：{next_value}（+{next_value-value}）" if next_value else "已超出下一档计算范围。", ""])
    return panel("副属性换算", lines, subtitle="7.x · Lv.100", footer="Allagan Studies 公式；这是属性换算，不是职业配装最优解。")


INDIGO = ["BD","TD","ND","RD","BS","TS","NS","RS","BN","TN","NN","RN"]
RUBY_OLD = ["OD","RD","OS","RS","ON","RN"]
RUBY_75 = ["TD","OD","TD","RD","TS","OS","TS","RS","TN","ON","TN","RN"]
STOPS = {
    "indigo": {"B":["谢尔达莱群岛近海","梅尔托尔海峡北","绯汐海近海"],"T":["谢尔达莱群岛近海","罗塔诺海海面","罗斯利特湾近海"],"N":["梅尔托尔海峡南","加拉迪翁湾外海","梅尔托尔海峡北"],"R":["加拉迪翁湾外海","梅尔托尔海峡南","罗塔诺海海面"]},
    "ruby": {"O":["Sirensong Sea","Kugane Coast","One River"],"R":["Sirensong Sea","Kugane Coast","Ruby Sea"],"T":["Unnamed Margin","Sirensong Sea","Thavnairian Coast"]},
}
TIMES = {"D":["夕","夜","日"],"S":["夜","日","夕"],"N":["日","夕","夜"]}
INDIGO_GOALS = {
    "BN":"只有我最鳐摆", "BD":"横路不通", "TN":"气鲀四海",
    "TD":"气鲀四海 / 只有我最鳐摆", "NN":"八爪旅人",
    "NS":"龙马惊神", "RD":"捕鲨人",
}


def route_at(timestamp: float, route: str = "indigo") -> str:
    if route not in {"indigo","ruby74","ruby75"}:
        raise ToolError("未知海钓版本。")
    base = INDIGO if route == "indigo" else (RUBY_OLD if route == "ruby74" else RUBY_75)
    index = (int(timestamp)//7200+88) % (len(base)*12)
    return base[(index % 12+index//12) % len(base)]


def ofish(raw: str, now: float | None = None) -> str:
    args = raw.split()
    if args == ["help"]:
        return help_panel("海钓时刻表", ["/ofish [班数，1～5]", "/ofish 靛青 3", "/ofish 红玉旧 3（7.5 前）", "/ofish 红玉新 3（7.5 起）"], footer="默认靛青；红玉请按所在服务器版本选择，不猜测国服更新进度。")
    route, count = "indigo", 3
    names = {"靛青":"indigo","indigo":"indigo","红玉旧":"ruby74","ruby74":"ruby74","红玉新":"ruby75","ruby75":"ruby75"}
    if args and args[0] in names:
        route = names[args.pop(0)]
    if len(args)>1:
        raise ToolError("用法：/ofish 靛青 3；红玉需指定 红玉旧 或 红玉新。")
    if args:
        count = integer(args[0],1,5)
    now = time.time() if now is None else now
    first = int(now)//7200*7200
    if now >= first+900:
        first += 7200
    lines = []
    for i in range(count):
        ts = first+i*7200
        code = route_at(ts,route)
        stops = STOPS["indigo" if route=="indigo" else "ruby"][code[0]]
        lines += [f"{stamp(ts)}～{stamp(ts+900)[-5:]} · {code}" + (" · 正在报名" if ts<=now<ts+900 else ""), *[f"{j+1}. {place}（{day}）" for j,(place,day) in enumerate(zip(stops,TIMES[code[-1]]))]]
        if route == "indigo" and code in INDIGO_GOALS:
            lines.append("可关注成就："+INDIGO_GOALS[code])
        lines.append("")
    return panel("海钓航班 · "+{"indigo":"靛青","ruby74":"红玉 / 7.5 前","ruby75":"红玉 / 7.5 起"}[route], lines, subtitle="北京时间 UTC+8 · 每两小时开放报名", footer="报名截止 :15；成就仍需满足鱼种/数量等条件，幻海流不保证。红玉新海域暂用英文原名。")
