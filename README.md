# FF14 插件

这是 [ff14-ddl-qbot 主体](https://github.com/lchlzk/ff14-ddl-qbot) 的独立安装包，合并三个 FF14 功能入口：

- `plugins.ffxiv_tools`：FF14 实用查询、合成等命令；
- `plugins.fflogs`：FF Logs 战斗记录查询；
- `plugins.otterbot`：獭獭工具与国服市场比价。

先安装并配置机器人主体，再在同一个 Python 环境安装本仓库：

```bash
python -m pip install 'git+https://github.com/lchlzk/ff14-ddl-qbot-plugin-ff14.git'
```

机器人启动时分别加载上述三个 `plugins.*` 模块；不要求安装 B站或嘟嘟脸插件。共享的 `bot_tools`、`message_ui` 和运行数据仍由主体提供。

`src/plugins/data/cn_market_items.tsv` 是国服可交易物品名称索引；文件首行注明来源，市场挂单与成交数据运行时由 Universalis 查询。游戏资料与第三方服务内容的权利归原权利人所有。

许可证：AGPL-3.0-only。
