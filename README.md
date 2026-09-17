# FF14 插件

这是 [ff14-ddl-qbot 主体](https://github.com/lchlzk/ff14-ddl-qbot) 的完整 FF14 安装包（0.2.0），含市场/制作、FF Logs、副属性、海钓及手动狩猎。三个加载入口为：

- `plugins.ffxiv_tools`：FF14 实用查询、合成等命令；
- `plugins.fflogs`：FF Logs 战斗记录查询；
- `plugins.otterbot`：獭獭工具与国服市场比价。

先安装并配置机器人主体，再在同一个 Python 环境安装本仓库：

```bash
python -m pip install --upgrade 'git+https://github.com/lchlzk/ff14-ddl-qbot-plugin-ff14.git'
```

机器人启动时分别加载上述三个 `plugins.*` 模块，重启后生效；不要求安装 B站或嘟嘟脸插件。请同步更新主体，以提供通用群设置扩展接口并移除旧工具箱中的重复命令。Docker 部署需更新插件版本锁定文件并重建镜像。插件仍使用主体的权限、数据库连接、缓存和消息排版公共服务，不适用于任意 NoneBot 项目。

## 常用命令

```text
/ff14
/market 物品名 国服全大区
/sales 物品名 服务器
/cheapest 物品名 大区
/recip 物品名
/craftcost 物品名 大区
/gather 物品名
/fflogs help
/dps 角色名 服务器
/raid 角色名 服务器
/fsx 暴击 3000
/ofish 靛青 3
/ofish 红玉旧 3
/ofish 红玉新 3
/group server 梦羽宝境
/hunt rule 怪物 4 6
/hunt kill 怪物
/hunt check 怪物
/hunt undo 怪物
```

`/fsx` 为 7.x、100 级属性换算；海钓红玉旧/新分别对应 7.5 前/起，需要按所在服务器版本选择。狩猎为本群手工记录，不是自动实时通报；配置和击杀/撤销需要群管理权限。`/command disable ff14` 可关闭整组功能。其余天气、房屋、任务等功能与参数由 `/ff14` 及各命令 `help` 查看。

`/group server`、狩猎规则和群设置总览由本插件注册到主体的公共扩展接口；未安装插件时不再提供这些功能。原有 `server`、`hunt_rules`、`hunt_kills` 数据键不变，升级保留各群记录，卸载不删除数据。

## 可选外部服务配置

按使用的查询功能在主体 `.env` 配置；不要把真实凭据提交到仓库：

```dotenv
OTTER_API_QQ=
OTTER_API_TOKEN=
OTTER_API_BASE=https://xn--v9x.net/api/
FFLOGS_CLIENT_ID=
FFLOGS_CLIENT_SECRET=
FFLOGS_DEFAULT_REGION=CN
```

本地副属性、海钓和狩猎不要求这些 API 凭据。查询服务权限、可用区域与限制由各外部服务决定。

## 源码、资源与测试

- `src/plugins/fflogs.py`、`ffxiv_tools.py`、`otterbot.py`：市场、制作、战绩与查询实现。
- `src/qbot_ff14/games.py`：副属性和海钓计算。
- `src/qbot_ff14/hunt.py`：持久化狩猎记录。
- `src/qbot_ff14/commands.py`：上述本地工具的命令与权限检查。
- `src/qbot_ff14/integration.py`：群设置、总览及管理信息。
- `src/qbot_ff14/catalog.py`：完整 FF14 功能目录。
- `tools/build_cn_market_index.py`：物品索引生成工具。
- `tests/`：接口、计算、权限、持久化、资源打包及索引测试。

在已配置主体的环境下，将主体源码目录加入 `PYTHONPATH`，执行 `python -m unittest discover -s tests`。

索引重建：`python tools/build_cn_market_index.py Item.csv marketable.json src/plugins/data/cn_market_items.tsv --source-commit 上游版本号`。副属性公式参考 Allagan Studies；海钓轮换资料参考 OceanTrip 与 OtterBot，计算实现独立编写，未引入其他机器人的运行依赖。

`src/plugins/data/cn_market_items.tsv` 是国服可交易物品名称索引；文件首行注明来源，市场挂单与成交数据运行时由 Universalis 查询。游戏资料与第三方服务内容的权利归原权利人所有。

许可证：AGPL-3.0-only。
