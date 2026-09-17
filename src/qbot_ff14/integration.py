"""Plugin-owned group settings, retaining existing document keys."""
from bot_tools import group_extensions
from bot_tools.storage import ToolError, clean
from message_ui import panel


def summary(doc: dict) -> dict:
    rules = doc.get("hunt_rules")
    return {
        "server": str(doc.get("server") or ""),
        "hunt_rules": len(rules) if isinstance(rules, dict) else 0,
    }


def overview(doc: dict) -> list[str]:
    data = summary(doc)
    return [
        f"狩猎默认小区：{data['server'] or '未设置'}",
        "  设置：/group server 梦羽宝境（供 /hunt 省略小区时使用）",
        f"狩猎规则：{data['hunt_rules']} 条",
        "  狩猎：/hunt rule 怪物 最早小时 最晚小时",
    ]


def configure(doc: dict, args: list[str]) -> str | None:
    if not args or args[0] != "server":
        return None
    if len(args) != 2:
        raise ToolError("用法：/group server 小区名")
    doc["server"] = clean(args[1], 30)
    return panel("设置已保存", "狩猎默认小区：" + doc["server"])


def register() -> None:
    group_extensions.register("ff14", group_extensions.GroupExtension(
        help_lines=("狩猎默认小区 · /group server 小区名", "狩猎 · /hunt rule · /hunt kill · /hunt undo"),
        overview=overview, configure=configure, summary=summary,
        admin_fields=lambda doc: [{"name": "server", "label": "狩猎默认小区",
                                   "value": summary(doc)["server"], "max_length": 30,
                                   "placeholder": "例如 梦羽宝境"}],
        admin_details=lambda doc: [{"label": "狩猎规则", "value": f"{summary(doc)['hunt_rules']} 条"}],
        admin_update=admin_update, search_keys=("server",),
    ))


def admin_update(doc: dict, data: dict) -> None:
    if "server" in data:
        server = str(data["server"]).strip()
        if server:
            doc["server"] = clean(server, 30)
        else:
            doc.pop("server", None)
