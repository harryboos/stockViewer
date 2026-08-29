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

- 直接访问股票或足球子域名时，应先经过私人首页鉴权。
- 三个域名都应使用有效 HTTPS，且公网不能访问 3000/8000 端口。
- 股票页面右上角应显示真实数据源，AI 卡片应显示已完成或明确的待配置状态。
- 定期备份 `stockViewer/data`、足球应用数据以及 `/opt/private-apps/config/private-apps.env`。
- 不要执行 `docker compose down -v`，它可能删除 Caddy 的证书与配置卷。

完整的入口配置、阿里云镜像中转和故障排查以 `private-apps-gateway/README.md` 为准。
