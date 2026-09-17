"""FF14-specific command directory."""
from message_ui import help_panel

def ff14_directory() -> str:
    return help_panel("FF14 插件 · 查询目录", [
        "🛒 市场与制作",
        "/market 物品名 服务器/国服全大区 · 市场价格与全区比价",
        "/sales 物品名 服务器 · 最近成交",
        "/cheapest 物品名 大区 · 跨服比价",
        "/recip 物品名 · 配方",
        "/craftcost 物品名 大区 · 制作成本",
        "/gather 物品名 · 采集地点",
        "",
        "⚔ 战绩与角色工具",
        "/dps 角色名 服务器 [副本简称] · 排名",
        "/raid 角色名 服务器 [副本简称] · 通关记录",
        "/fflogs · 战绩详细帮助",
        "/fsx 暴击 3000 · 副属性计算",
        "",
        "🎮 游戏资料与活动",
        "/quest 任务名 · /search 物品名",
        "/weather 区域 [天气] [数量]",
        "/house 服务器 [区域] [大小] [部队|个人]",
        "/ofish · 海钓班次",
        "/hunt · 手动狩猎时钟（非实时数据）",
        "/luck · 今日运势 /gate [2|3] · 挖宝选门",
    ], footer="直接输入具体命令查询，命令后加 help 查看参数。部分接口需要管理员配置凭据。机器人全部功能：/toolbox。")
