# 私人服务器部署

线上入口、HTTPS、访问密钥和 Docker Compose 已统一由独立的 `private-apps-gateway` 项目管理。`stockViewer` 只保存股票应用源码、数据目录和镜像构建文件，真实密钥不放在任何 Git 仓库中。

## 服务器目录

```text
/opt/private-apps/
├── private-apps-gateway/
├── stockViewer/
├── legend-football-manager/
└── config/
    └── private-apps.env
```

四个目录应保持同级。股票数据库位于 `/opt/private-apps/stockViewer/data/stockviewer.sqlite3`；服务器环境变量位于 `/opt/private-apps/config/private-apps.env`，建议权限设置为 `600`。

## 首次部署

分别取得三个项目后，在 gateway 项目中准备仓库外配置：

```bash
cd /opt/private-apps/private-apps-gateway
mkdir -p ../config
cp env.example ../config/private-apps.env
chmod 600 ../config/private-apps.env
```

编辑 `/opt/private-apps/config/private-apps.env`，填写三个域名、入口访问密钥、会话密钥、镜像地址和需要启用的 AI API Key。随后执行：

```bash
./scripts/check.sh
./scripts/deploy.sh
```

公网只应开放 SSH、TCP 80、TCP 443 和 UDP 443，不要开放应用内部的 3000 或 8000 端口。

## 更新股票应用

```bash
cd /opt/private-apps/stockViewer
git pull --ff-only
cd ../private-apps-gateway
./scripts/deploy.sh stock-viewer
```

这会重新构建并替换股票容器，不会删除 `stockViewer/data` 中的 SQLite 数据。修改 `/opt/private-apps/config/private-apps.env` 后也需要重新执行同一条部署命令，新的环境变量才会进入容器。

查看运行状态和日志：

```bash
cd /opt/private-apps/private-apps-gateway
docker compose --env-file ../config/private-apps.env ps
docker compose --env-file ../config/private-apps.env logs --tail=200 stock-viewer
```

## 更新首页或路由

```bash
cd /opt/private-apps/private-apps-gateway
git pull --ff-only
./scripts/deploy.sh
```

## AI 配置

服务器配置使用以下变量名：

```dotenv
STOCK_DEEPSEEK_API_KEY=
STOCK_DEEPSEEK_MODEL=deepseek-chat

GLM_API_KEY=
GLM_MODEL=glm-5.3
GLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4

QWEN_API_KEY=
QWEN_MODEL=qwen3.8-max
QWEN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
```

不要再创建 `stockViewer/deploy/.env.server`。如果当天 AI 运行失败，修复配置或部署新版后，在策略页再次点击“今日策略状态”即可重试；当天已成功的模型不会重复计费调用。

## 验收与备份

### 历史预测一直没有收益数据

先区分观察时间和数据错误：预测从生成日次日起算，只有首个完整交易日收盘后才会出现阶段收益。已经过了首个收盘日却仍无数据时，查看股票容器日志中的「预测反馈交易日历读取/解析失败」或「历史预测反馈更新失败」。不要删除预测记录或重新调用 AI 来处理行情问题。

旧版反馈代码使用 `with MiniRacer()` 解析交易日历。AKShare 在 Linux 安装的 `py-mini-racer 0.6` 不支持该上下文管理接口，而 macOS 使用的 `mini-racer` 支持，因而可能出现本地正常、Linux 容器整批反馈失败。现已改成兼容两种运行时的调用与资源释放方式；保留底层错误日志，同时在页面区分等待收盘、等待更新与读取失败。接口差异可核对 [PyMiniRacer 0.6 源码](https://github.com/sqreen/PyMiniRacer/blob/v0.6.0/py_mini_racer/py_mini_racer.py)。

拉取新版股票代码并按上文重建 `stock-viewer` 容器，启动时会自动补查，也可在预测 Tab 点击「更新实际表现」。这一步只读取历史行情，不额外调用 AI；已有预测档案保留。

行情源可能间歇断连。已经取得的阶段行情也会保留其实际截止日期，并标注此次更新失败，后续继续补查；旧阶段收益不会因窗口到期被直接当成最终收益。尚未取得任何行情的记录保持缺失，不填零或虚构收益。

东财概念历史断连时，可选为股票后端配置 `TUSHARE_TOKEN`，账号须具备 `dc_daily` 权限。gateway 如使用环境变量白名单，需要将仓库外配置中的同名变量传给 `stock-viewer`，再重新部署；本项目不会自动读取主机上未注入容器的变量。可在容器内执行 `npm run probe:sources -- --start 2026-09-18 --end 2026-09-21`，确认真实日期覆盖后在页面更新实际表现与同期最强。检查工具不写数据库、不调用 AI，详情见[数据源实测与限制](data-sources-2026-09-22.md)。

### 常规检查

- 直接访问股票或足球子域名时，应先经过私人首页鉴权。
- 三个域名都应使用有效 HTTPS，且公网不能访问 3000/8000 端口。
- 股票页面右上角应显示真实数据源，AI 卡片应显示已完成或明确的待配置状态。
- 定期备份 `stockViewer/data`、足球应用数据以及 `/opt/private-apps/config/private-apps.env`。
- 不要执行 `docker compose down -v`，它可能删除 Caddy 的证书与配置卷。

完整的入口配置、阿里云镜像中转和故障排查以 `private-apps-gateway/README.md` 为准。
