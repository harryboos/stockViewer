# 私人服务器部署

这套部署会在同一台服务器上运行一个中文私人首页、观星 A股和传奇足球经理，并用同一把访问密钥保护三个站点。

## 访问结构

```text
互联网
  │
  ▼
Caddy（只公开 80 / 443，自动 HTTPS）
  ├── home.example.com       私人首页与访问密钥验证
  ├── stocks.example.com     观星 A股
  └── football.example.com   传奇足球经理
             │
             ▼
      Docker 内部网络（不公开应用端口）
```

访问首页并验证成功后，浏览器会得到一个覆盖根域名的 HttpOnly、Secure、SameSite=Lax 会话 Cookie。股票和足球两个子域名都会先经过入口服务校验，因此不能通过直接输入子域名绕过访问密钥。入口同时签发足球应用原有的外层访问令牌，进入足球应用后只需要登录足球经理账号，不会再次要求输入朋友访问密钥。

## 服务器与域名要求

- 一台可以运行 Docker Engine 和 Docker Compose 的 Linux 服务器，建议至少 2 核 CPU、4 GB 内存、20 GB 可用磁盘。
- 一个自己管理 DNS 的域名，以及三个指向服务器公网 IP 的 A 记录；有 IPv6 时也可以增加 AAAA 记录。
- 公网只开放 SSH、TCP 80、TCP 443 和 UDP 443。不要开放 3000 或 8000。
- 服务器需要能访问东方财富、AKShare/BaoStock 相关上游和你启用的 AI 接口。

Caddy 只有在域名已经解析到服务器、80/443 可以从公网访问时，才能自动申请 HTTPS 证书。具体要求可参考 [Caddy HTTPS 快速入门](https://caddyserver.com/docs/quick-starts/https)。

## 目录摆放

两个项目必须作为同级目录放在服务器上，目录名保持如下：

```text
/opt/private-apps/
├── stockViewer/
└── legend-football-manager/
```

可以分别从 Git 仓库克隆，也可以把本机目录上传到服务器。不要上传 `.env.local`、`.venv`、`node_modules` 或包含旧密钥的文件。

## 配置环境变量

在服务器进入 `stockViewer` 目录后执行：

```bash
cp deploy/server.env.example deploy/.env.server
chmod 600 deploy/.env.server
openssl rand -hex 16
openssl rand -hex 32
```

编辑 `deploy/.env.server`：

```dotenv
PORTAL_DOMAIN=home.your-domain.com
STOCK_DOMAIN=stocks.your-domain.com
FOOTBALL_DOMAIN=football.your-domain.com
COOKIE_DOMAIN=your-domain.com

SITE_ACCESS_KEY=粘贴第一条命令生成的32位随机字符串
PORTAL_SESSION_SECRET=粘贴第二条命令生成的64位随机字符串
```

注意：

- `COOKIE_DOMAIN` 只写根域名，不要写 `https://`、路径、端口或开头的点。
- `PORTAL_SESSION_SECRET` 必须和 `SITE_ACCESS_KEY` 不同。
- AI 密钥只填在服务器的 `deploy/.env.server`，不要写进源码、Dockerfile 或 Git。
- 股票和足球应用可以使用不同的 DeepSeek Key，分别填写 `STOCK_DEEPSEEK_API_KEY` 和 `FOOTBALL_DEEPSEEK_API_KEY`。
- 修改 `SITE_ACCESS_KEY` 或 `PORTAL_SESSION_SECRET` 后，已登录浏览器的入口会话立即失效。

## Docker Hub 拉取失败

如果日志包含 `registry-1.docker.io:443`、`connection refused`，随后又出现 `No such image`，根因是服务器无法从 Docker Hub 拉取第一个基础镜像；后面的镜像会被中断，这不是 Caddy 或 Node 标签不存在。

### 推荐：使用同地域的阿里云 ACR

个人项目可以使用 ACR 个人版作为免费镜像中转。先在容器镜像服务中创建与 ECS 同地域的个人版实例、命名空间，以及 `caddy`、`node` 两个私有仓库。然后在一台能正常访问 Docker Hub 的电脑上执行以下命令，把占位内容替换成 ACR 控制台“访问凭证”和仓库页面显示的实际地址：

```bash
docker pull caddy:2.11.4-alpine
docker pull node:22-bookworm-slim

docker login --username='<ACR 登录名>' '<ACR 实例域名>'
docker tag caddy:2.11.4-alpine '<ACR 实例域名>/<命名空间>/caddy:2.11.4-alpine'
docker tag node:22-bookworm-slim '<ACR 实例域名>/<命名空间>/node:22-bookworm-slim'
docker push '<ACR 实例域名>/<命名空间>/caddy:2.11.4-alpine'
docker push '<ACR 实例域名>/<命名空间>/node:22-bookworm-slim'
```

回到 ECS，用同一个实例域名登录。不要把 Registry 密码写进 `.env.server` 或发到聊天中：

```bash
docker login --username='<ACR 登录名>' '<ACR 实例域名>'
```

在 `deploy/.env.server` 增加：

```dotenv
CADDY_IMAGE=<ACR 实例域名>/<命名空间>/caddy:2.11.4-alpine
NODE_IMAGE=<ACR 实例域名>/<命名空间>/node:22-bookworm-slim
```

部署文件会把 `NODE_IMAGE` 同时用于首页和股票应用的构建基础镜像，以及足球应用的运行镜像。企业版 ACR 用户也可以用“制品订阅”直接把 `library/caddy` 和 `library/node` 的指定标签同步到同地域仓库，无需从本机推送。

### 临时方案：阿里云专属镜像加速地址

也可以在 ACR 控制台的“镜像工具 > 镜像加速器”复制本账号专属地址，并将它合并到 `/etc/docker/daemon.json`：

```json
{
  "registry-mirrors": ["https://<你的专属地址>.mirror.aliyuncs.com"]
}
```

如果文件中已有其他 Docker 配置，保留原字段，只增加 `registry-mirrors`，不要整文件覆盖。保存后执行：

```bash
sudo systemctl daemon-reload
sudo systemctl restart docker
docker info
docker pull caddy:2.11.4-alpine
docker pull node:22-bookworm-slim
```

阿里云目前说明镜像加速器已停止同步最新镜像，因此这条路径只能作为快速尝试；指定标签仍拉不到时，改用上面的 ACR 仓库方案。

## 首次启动

先检查最终配置，再构建并启动：

```bash
docker compose --env-file deploy/.env.server -f deploy/compose.server.yml config --quiet
docker compose --env-file deploy/.env.server -f deploy/compose.server.yml up -d --build
docker compose --env-file deploy/.env.server -f deploy/compose.server.yml ps
```

查看启动日志：

```bash
docker compose --env-file deploy/.env.server -f deploy/compose.server.yml logs -f --tail=200
```

首次构建股票应用需要安装 Node 和 Python 依赖，通常会比足球应用慢。三个应用健康后，打开 `https://你的首页域名`，输入 `SITE_ACCESS_KEY`，再分别测试两张应用卡片。

## 验收清单

- 直接访问股票或足球子域名时，会被带回私人首页。
- 输入错误密钥会提示失败；连续输错 5 次会锁定该来源 10 分钟。
- 输入正确密钥后，两张应用卡片都能打开，不需要再次输入外层密钥。
- 足球应用仍然要求自己的账号登录；这是应用内的用户/球队权限，不是重复的外层密钥。
- 股票页面的 `/api/system` 显示数据服务正常，数据目录中出现 `stockviewer.sqlite3`。
- `docker compose ps` 中四个服务均为运行或健康状态。
- 从公网无法访问服务器的 3000 和 8000 端口。

## 数据保存与备份

需要备份的持久数据都保留在项目目录：

```text
stockViewer/data/stockviewer.sqlite3
legend-football-manager/data/games.json
legend-football-manager/data/auth.json
```

建议每天备份两个 `data` 目录，并把备份复制到另一台机器或对象存储。备份前可短暂停止应用以获得最一致的文件快照：

```bash
docker compose --env-file deploy/.env.server -f deploy/compose.server.yml stop stock-viewer legend-football-manager
tar -czf private-apps-data-backup.tar.gz ../stockViewer/data ../legend-football-manager/data
docker compose --env-file deploy/.env.server -f deploy/compose.server.yml start stock-viewer legend-football-manager
```

不要把带有账号、房间或自选股数据的备份提交到公开仓库。

## 更新与重启

更新两个项目代码后，在 `stockViewer` 目录重新运行：

```bash
docker compose --env-file deploy/.env.server -f deploy/compose.server.yml up -d --build
docker image prune
```

单纯修改 `deploy/.env.server` 后也需要重新执行 `up -d`，Docker 才会把新环境变量注入容器。常用状态命令：

```bash
docker compose --env-file deploy/.env.server -f deploy/compose.server.yml ps
docker compose --env-file deploy/.env.server -f deploy/compose.server.yml restart
docker compose --env-file deploy/.env.server -f deploy/compose.server.yml logs --tail=200 portal caddy
```

`docker image prune` 只清理未使用镜像，不会删除应用数据；执行任何清理命令前仍建议先确认备份可用。

## 安全边界

共享访问密钥适合个人或少量受信任用户访问，不等同于企业身份系统。若以后需要为不同访客单独授权、撤销某一个人的权限或记录登录审计，应把共享密钥升级为独立账号、双因素认证或专用身份网关。

反向代理使用 Caddy 的 `forward_auth` 在请求进入应用前调用入口鉴权服务；非成功响应会直接返回给浏览器。行为说明见 [Caddy forward_auth 文档](https://caddyserver.com/docs/caddyfile/directives/forward_auth)。
