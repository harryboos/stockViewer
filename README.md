# 观星 A股

可在本地或私人服务器运行的简体中文 A 股观察台。它可以管理自选股、读取免费真实行情、按公开方法计算量化策略，并让 DeepSeek、Gemini、ChatGPT 各自每天完成一次 AI 选股。

![观星 A股预览](public/og.png)

> 本项目只用于个人研究。部署到私人服务器时请启用访问保护；内容不构成投资建议或收益承诺。

## 这次升级解决了什么

- A 股行情不再依赖 Tushare Token 或积分
- 主数据源使用 AKShare；主源失败时自动降级到 BaoStock
- 股票目录、自选股、行情缓存和策略结果保存在本机 SQLite
- 默认包含 15 分钟行情缓存、东方财富备用线路、失败降级、最后成功数据保留和明确错误状态
- 三家 AI 共享完全相同的系统提示语、任务提示语、真实候选池和输出约束；同一模型同一天默认只运行一次
- 规则策略增加趋势确认、价值动量和热门概念共振，热门概念使用当日实时板块强度与上涨广度
- 从 GitHub 维护清晰的量化仓库适配 Qlib 精简多因子、双均线趋势与 RSI 超跌回升
- 策略页底部自动汇总多策略共识、规则覆盖、AI 完成度和数据降级提醒
- 网站运行时会在北京时间工作日 18:10 自动执行每日策略

免费数据源没有服务等级保证，页面给出的价格仍应在交易前向券商核对。

## 技术架构

```text
中文 React 界面（Vinext / TypeScript）
                │
                ▼
本地接口转发（同源 /api）
                │
                ▼
FastAPI 数据服务（Python）
       ├── AKShare：复权历史、估值与标准东方财富线路
       ├── 东方财富备用线路：实时快照、概念板块与成份股（绕过异常系统代理）
       ├── BaoStock：股票目录、历史日线、故障降级
       ├── SQLite：自选股、缓存、每日结果
       └── DeepSeek / Gemini / OpenAI 官方接口
```

前端继续保留原有界面，只把更适合 A 股数据生态的部分迁到了 Python。因此不用重做整个网站，也更方便以后加入回测、财务因子和机器学习。

代码按职责拆分：页面组件不直接保存策略元数据，行情服务不直接实现 HTTP 分页，策略编排不再混合技术指标计算。环境变量统一由 `backend/config.py` 读取并限制在合理范围，外部请求与降级逻辑也可以独立测试。

## 环境要求

- Node.js 22.13.0 或更高版本
- Python 3.11～3.13
- 可以访问所选免费行情源的网络
- 只有启用 AI 选股时才需要对应模型的 API 密钥

## 第一次安装

```bash
npm install
npm run setup
```

`npm run setup` 会在项目内创建 `.venv`，并安装 FastAPI、AKShare、BaoStock 等依赖。

## 启动

```bash
npm run dev
```

打开 <http://localhost:3000>。这一个命令会同时启动中文网站和本地数据服务。

第一次读取行情或股票目录可能需要 10～30 秒；以后会直接使用缓存。数据库文件位于 `data/stockviewer.sqlite3`。

## 私人服务器部署

项目现在提供一个统一私人首页，可在验证一次共享访问密钥后进入“观星 A股”和同级目录中的 `legend-football-manager`。部署使用三个同域子域名、Caddy 自动 HTTPS、共享 HttpOnly 会话和 Docker Compose；股票与足球应用端口只在 Docker 内部网络中开放。

完整的域名、环境变量、启动、验收与备份步骤见 [私人服务器部署文档](docs/server-deployment.md)。服务器专用密钥填写在 `deploy/.env.server`，不要填写到本地 `.env.local` 或提交到 Git。

## AI 配置

复制 `.env.example` 为 `.env.local`，只填写需要启用的模型：

```dotenv
DEEPSEEK_API_KEY=
DEEPSEEK_MODEL=deepseek-chat

GEMINI_API_KEY=
GEMINI_MODEL=gemini-2.5-flash

OPENAI_API_KEY=
OPENAI_MODEL=gpt-5.4-mini
```

股票行情本身不需要密钥。AI 接口可能产生供应商费用；未配置的模型会显示“待配置”，不会使用演示结果冒充。

DeepSeek、Gemini、ChatGPT 收到完全相同的系统提示语、任务说明、候选快照与 JSON 字段约束。三张卡片只保留模型供应商本身的差异；修改提示语后，旧版本当天缓存会自动失效并按新提示语重新运行一次。

## 免费行情如何工作

| 顺序 | 数据源 | 主要用途 | 特点 |
| --- | --- | --- | --- |
| 1 | 东方财富 `push2delay` 备用线路 | A 股行情快照、概念板块与成份股 | 默认绕过系统代理，采用浏览器请求头、分页限速和自动重试 |
| 2 | [AKShare](https://akshare.akfamily.xyz/) | 标准行情线路、前复权历史、估值和股息率 | 备用线路不可用时再尝试，避免每次先触发已知代理错误 |
| 3 | [BaoStock](https://www.baostock.com/) | 股票目录、历史日线、PE/PB、最终故障降级 | 无需密钥、自有服务器；以日线为主，不是盘中实时源 |

实时行情会优先访问与 AKShare 字段兼容的东方财富备用域名，并先绕过操作系统代理；若网络必须使用代理，会自动再尝试系统线路。备用域名失败后才调用 AKShare 标准接口，最后再降级到 BaoStock 最近交易日日线。界面右上角会标出当前实际使用的数据源。

全市场快照默认缓存 15 分钟，主线路失败后 30 分钟内不重复冲击上游。可以通过 `SPOT_CACHE_SECONDS`、`PRIMARY_FAILURE_BACKOFF_SECONDS`、`EASTMONEY_PAGE_DELAY_SECONDS` 调整。除非排查问题，不建议关闭 `EASTMONEY_DELAY_ENABLED`。

## 每日规则策略

这些策略不是复制官方指数，而是在当前流动性候选池中借鉴公开编制思路重新排序；强势缩量为用户自定义严格筛选，热门概念使用独立的实时概念板块候选池：

| 策略 | 实际因子 | 公开方法来源 |
| --- | --- | --- |
| 红利低波 / 价值低波 | 股息率可用时纳入；否则使用正 PE、PB 与 120 日波动降级 | [上证红利质量指数方法](https://www.sse.com.cn/market/sseindex/diclosure/c/c_20250123_10770595.shtml) |
| 价格动量 | 近半年动量、近一年动量、成交流动性 | [创业板动量成长指数方法](https://www.cnindex.com.cn/docs/gz_399296.pdf) |
| 小盘低波 | 120 日收益波动、市值、成交流动性 | [巨潮小盘低波指数方法](https://www.cnindex.com.cn/docs/gz_399408.pdf) |
| Qlib 精简多因子 | 5/20/60 日收益、MA20 偏离、5/20 日量能、波动和流动性 | [microsoft/qlib Alpha158](https://github.com/microsoft/qlib/blob/main/qlib/contrib/data/loader.py) |
| 双均线趋势 | 现价＞MA10＞MA30、20 日收益为正、均线强度、波动和流动性 | [kernc/backtesting.py SmaCross](https://github.com/kernc/backtesting.py/blob/master/doc/examples/Strategies%20Library.py) |
| RSI 超跌回升 | 14 日 Wilder RSI 三日前低于 45、当前回升但低于 60、5 日收益为正、现价高于 MA5 | [QuantConnect LEAN RsiAlphaModel](https://github.com/QuantConnect/Lean/blob/master/Algorithm.Framework/Alphas/RsiAlphaModel.py) |
| 趋势确认 | 现价＞MA20＞MA60、20/60 日动量为正、低波动、成交流动性 | [创业板动量成长指数方法](https://www.cnindex.com.cn/docs/gz_399296.pdf) |
| 价值动量 | 正 PE、正 PB、60 日与半年动量、成交流动性 | [国证 AlphaFocus 单因子指数系列](https://www.cnindex.com.cn/zh_information/notices_news/2021/202105/t20210525_17317.html?act_menu=2) |
| 强势缩量筛选 | 昨额＞10亿、10日涨幅＞30%、20日均额＞12亿、量比＜1.5、今日涨幅＜11%、现价＜90元、非 ST、非科创板 | 用户自定义规则 |
| 热门概念共振 | 当日概念涨幅、上涨家数占比、个股涨幅和成交额；剔除 ST、退市整理、涨幅≥11% 和成交额不足 1 亿元 | [AKShare 概念板块与成份股接口](https://akshare.akfamily.xyz/data/stock/stock.html) |

候选样本数默认是 18，可以在 `.env.local` 通过 `STRATEGY_SAMPLE_SIZE` 调整为 12～40。热门概念默认取强度靠前的 5 个，可以通过 `HOT_CONCEPT_LIMIT` 调整为 3～8。样本越多，第一次计算越慢，也越容易触发免费源限流。

热门概念要求板块当日涨幅为正且上涨家数多于下跌家数，再对成份股排序。当天成功结果写入 SQLite；若实时概念源不可用且当天没有成功缓存，页面显示空结果，不会把旧日期热点或模拟股票当作今日结果。

“最近 10 个交易日涨幅”按最新可用价格相对 10 个交易日前收盘价计算；“最近 20 个交易日日均成交额”包含最新交易日。AKShare 可用时直接采用盘中量比；降级到 BaoStock 日线时，量比按最新成交量 ÷ 前 5 日平均成交量估算。任一指标缺失时，该股票不会入选，也不会用接近条件的股票补位。

## GitHub 策略口径

项目只选择有明确源码、参数和维护主体的仓库，并优先采用能用现有 OHLCV 日线复现的方法。页面中的实现是针对 A 股候选池的透明适配，不是原项目完整模型或原样回测结果：

- Qlib 策略使用 Alpha158 公开的 ROC、MA、STD 与成交量滚动因子族，但不运行机器学习训练流程。
- 双均线策略沿用 SmaCross 的短长均线信号，将参数适配为 MA10/MA30 并增加流动性与波动排序。
- RSI 策略采用 LEAN 使用的 14 日 Wilder RSI；只有形成回升且未进入高位时才给出结果。

GitHub 星标、开源许可或示例代码都不等于策略有效性证明。新增方法仍需用 A 股历史数据做独立样本外回测，纳入手续费、滑点、涨跌停、停牌和幸存者偏差后，才能评价其稳定性。

## 页面总结

策略页最底部的“今日策略总结”会合并规则策略与已成功运行的 AI 结果。股票至少被两种独立方法选中才进入“多策略共识”；同时显示规则完成数、AI 完成数、不重复候选数量，以及热门概念、严格筛选和降级数据源的提示。共识只表示方法重合，不代表上涨概率或投资建议。

## 每日运行

网站保持运行时，内置任务会在北京时间工作日 18:10 自动执行。时间可以修改：

```dotenv
ENABLE_DAILY_SCHEDULER=true
DAILY_RUN_HOUR=18
DAILY_RUN_MINUTE=10
```

也可以手动运行：

```bash
npm run daily
```

若设置了 `DAILY_RUN_SECRET`，手动任务会自动携带密钥。当天已有成功结果时不会重复调用 AI；需要重新计算时可使用接口的 `force=true` 参数。

## 常用命令

| 命令 | 作用 |
| --- | --- |
| `npm run setup` | 创建 Python 环境并安装免费数据依赖 |
| `npm run dev` | 同时启动网站和数据服务 |
| `npm run build` | 构建前端生产版本 |
| `npm run start` | 启动构建后的本地产品 |
| `npm run lint` | 检查前端代码 |
| `npm run check:backend` | 检查 Python 服务语法 |
| `npm run test:backend` | 运行数据源、交易日和策略因子测试 |
| `npm run daily` | 手动执行当天策略 |

## 项目结构

```text
stockViewer/
├── app/                       # 中文界面与同源 API 转发
├── components/                # 页头、自选、策略和添加股票视图
├── lib/                       # 前端请求、类型、格式化与策略汇总
├── backend/
│   ├── main.py                # FastAPI、每日定时任务和接口
│   ├── config.py              # 环境配置与边界校验
│   ├── eastmoney.py           # 东方财富备用线路与分页重试
│   ├── data_sources.py        # AKShare / BaoStock 编排与自动降级
│   ├── database.py            # SQLite 数据与缓存
│   ├── strategy_factors.py    # RSI、均线、动量、百分位与严格筛选因子
│   ├── strategies.py          # 公开方法、自定义策略与运行编排
│   ├── ai.py                  # 三家 AI 官方接口与统一提示语
│   ├── test_data_sources.py   # 数据源、交易日和因子单元测试
│   └── requirements.txt
├── scripts/
│   ├── setup-python.mjs       # 一键安装本地数据服务
│   ├── local-stack.mjs        # 同时启动前后端
│   └── run-daily.mjs          # 手动每日任务
├── data/                      # 本地数据库，已被 Git 忽略
├── .env.example
└── package.json
```

## 本地接口

| 接口 | 用途 |
| --- | --- |
| `GET /api/system` | 数据源、AI 和定时任务状态 |
| `GET /api/watchlist` | 自选股及最新可用行情 |
| `POST /api/watchlist` | 添加自选股 |
| `DELETE /api/watchlist` | 删除自选股 |
| `GET /api/stocks/search?q=` | 搜索全部正常上市 A 股 |
| `GET /api/strategies/public` | 读取或计算今日公开策略 |
| `GET /api/strategies/ai` | 读取今日 AI 状态 |
| `POST /api/strategies/ai` | 运行今日三家 AI 选股 |

FastAPI 的本地调试文档位于 <http://127.0.0.1:8000/docs>。

## 常见问题

### 页面提示 AKShare 已降级

系统会依次尝试东方财富备用线路、AKShare 标准线路和 BaoStock。如果仍显示 BaoStock，通常是两个东方财富域名都被当前网络限流、拦截或上游接口再次调整。只要右上角有交易日期，网站仍在使用真实日线数据；页面会在失败冷却时间结束后自动重试实时源。

当前已知的 macOS `127.0.0.1` 系统代理问题由备用线路自动绕过，无需关闭整台电脑的代理。需要确认实际连接方式时，查看 `GET /api/system` 返回的 `dataSource.transport`、`health` 和 `error`。

### 搜索第一次很慢

首次会同步约 5,000 只正常上市 A 股；同步完成后搜索直接查询本地 SQLite。股票目录每天最多自动同步一次。

### 如何清空所有本地数据

先停止网站，再移动或删除 `data/stockviewer.sqlite3`。下次启动会创建新数据库并恢复默认自选股。此操作会清除自选股和历史策略结果，请先备份该文件。

### 是否适合商业交易系统

不适合。AKShare 和 BaoStock 适合个人研究与原型，没有交易级服务承诺。商业使用、自动下单或需要毫秒级行情时，应采购交易所授权数据并增加账户、审计、监控和灾备能力。
