# 行情数据源调研与实测（2026-09-22）

本次针对线上 9 月 17 日预测无收益、同期最强概念为空的问题，重点验证 2026-09-18 至 2026-09-21 的历史日线。测试从本机发出，不能代替线上服务器的连通性验收。未调用 AI、未写入真实预测记录、未购买任何行情服务。

## 实测结果

| 来源 | 实际查询 | 结果 | 本次处理 |
| --- | --- | --- | --- |
| 东方财富 `push2his` | BK1152 高带宽内存、BK1136 光通信模块、BK0917 半导体概念 | 三个概念均连接中断，未取得日线；旧 `pdfm2` 接口返回门户 HTML | 保留原线路；不把 HTTP 200 当作有效行情 |
| 腾讯证券 | 上证指数 sh000001、科创50 sh000688 | 两个指数各返回 9/18、9/21 两根有效日线 | 调整为基准优先源，避免先等待东财故障线路 |
| 新浪财经 | 同上 | 两个指数均成功，与腾讯点位差异小于 0.01 点，符合显示精度差异 | 已接入基准自动备用 |
| 同花顺 | 存储芯片 886042、共封装光学(CPO) 886033、芯片概念 885756 | 三个概念各返回两根有效日线 | 新增独立 `THS:` 适配器及诊断支持；未替代现有 BK 预测 |
| Tushare `dc_daily` | 官方 HTTPS 接口、BK1152.DC 查询格式 | 空凭证返回 40101；本地未配置 Token，**未完成授权行情实测** | 已按官方字段适配，完成模拟响应与故障切换测试；配置后可复测三个原概念 |
| 百度股市通 | 科创50日线、300308 所属概念 | HTTP 403，响应明确要求验证码 | 不接入自动降级，不绕过验证码 |
| BaoStock | sh.000001、sh.000688 | 登录成功；上证两根日线，科创50空列表 | 保留已有个股日线用途，本次不作为两项基准的统一备用源 |

9 月 21 日收盘点位核对：腾讯／新浪上证指数分别为 3949.910／3949.907，科创50为 1657.480／1657.485。这些是服务实际返回值，非测试生成数据。单次成功不代表持续可用或完整服务保证。

## 为什么不能用另一家“同名概念”补收益

东财 BK、同花顺 88xxxx、聚宽 GN、通达信板块及米筐特色指数拥有不同代码与编制范围。名称相近不足以证明成份、权重和历史点位一致。因此旧预测仍绑定原 BK 指数，不能以同花顺存储芯片冒充高带宽内存，也不能以股票等权收益冒充板块指数。

AKShare 与 ADATA 是数据获取工具，东方财富概念历史实现仍调用 `push2his.eastmoney.com`。它们不是独立的东财历史库存，不能通过换 Python 库绕过同一上游故障。

本次另查阅了聚宽、米筐、东财掘金与 Choice 官方资料。这些可用于后续独立概念研究、成份或授权行情接入，但本机没有对应账户，未进行授权行情测试，也未确认现有三个 BK 指数的等价覆盖。东财掘金文档中的 `BK.007001` 是掘金板块代码，不可因前缀相似就视为东方财富 `BK1152`。

## 已接入的行为

- 预测收益、同期最强和板块轮动共用历史读取路径，自动受益于新适配。
- BK 概念配置 `TUSHARE_TOKEN` 后优先查询 `dc_daily`，失败或无数据时回退原东财；未配置时继续免费模式。概念推荐、未来预测的历史技术指标也可使用该来源，并保留来源证据。
- 上证指数／科创50按腾讯 → 新浪 → 东方财富读取。只选择某一家返回的序列，不跨指数拼接；如果来源落后，会继续尝试下一家。
- 同花顺只接受显式 `THS:88xxxx` 代码，现有预测收益接口拒绝该命名空间，防止静默替换。
- 保留真实日期、开高低收、来源；收益核对仍要求原观察窗口完整，不填零，不把尚未结束的阶段收益算作最终命中率。
- Tushare 使用 HTTPS、禁止重定向、限时请求、进程内串行限速；凭证／权限错误冷却 10 分钟，网络／限频错误冷却 1 分钟。更换 Token 可解除旧凭证的冷却状态，错误输出不含密钥或上游原始响应。

## 配置与复测

本地 `.env.local` 或后端进程环境中配置 `TUSHARE_TOKEN`。官方 `dc_daily` 文档目前要求 **6000 积分**，普通注册 Token 不保证有权限。此配置可选，与 GLM 密钥无关，不需要安装 Tushare SDK。

```bash
npm run probe:sources -- --start 2026-09-18 --end 2026-09-21
# 单独核对授权后的原概念
npm run probe:sources -- --sources tushare --start 2026-09-18 --end 2026-09-21
# 验证产品实际使用的自动降级路径
npm run probe:sources -- --sources auto --start 2026-09-18 --end 2026-09-21
```

检查工具只读行情，不读取或修改预测数据库，也不调用 AI。输出区分 `available`、`empty`、`unavailable`、`not_configured`，包含实际日期、首末价格、耗时。`available` 表示取得有效日线，仍需对照交易日历确认窗口覆盖。

服务器请在股票容器的实际网络环境运行同一检查。若 gateway 的 Compose 使用环境变量白名单，还需将仓库外配置中的 `TUSHARE_TOKEN` 传给 `stock-viewer` 服务；仅在主机配置文件填写而没有传入容器不会生效。更新代码、注入凭证并重启后，再点击“更新实际表现”和“核对同期最强”，无需重新生成 AI 预测。

本次不能宣称线上三个概念已经补齐：免费东财仍断连，Tushare 尚缺具备权限的凭证。适配已就绪，授权查询的真实日期覆盖仍需验收。

## 主要资料

- [Tushare 东财概念板块日线：代码、OHLC、成交额单位、权限](https://tushare.pro/document/2?doc_id=382)
- [Tushare HTTP 请求与响应结构](https://tushare.pro/document/1?doc_id=40)
- [ADATA 东方财富概念历史实现](https://github.com/1nchaos/adata/blob/main/adata/stock/market/concepth_market/concept_market_east.py)
- [AKShare 同花顺概念指数实现](https://github.com/akfamily/akshare/blob/main/akshare/stock_feature/stock_board_concept_ths.py)
- [新浪科创50行情页](https://finance.sina.com.cn/realstock/company/sh000688/nc.shtml)、[腾讯科创50行情页](https://gu.qq.com/sh000688/zs)
- [聚宽行业概念与成份接口](https://www.joinquant.com/help/data/stock?f=home&m=footer)
- [米筐特色指数及历史行情](https://www.ricequant.com/doc/rqdata/python/ricequant-index)
- [东财掘金板块定义与日线](https://emquant.18.cn/help/doc/data/%E6%9D%BF%E5%9D%97.html)
- [Choice EMQuant API](https://quantapi.eastmoney.com/Upload/EMQuantAPI_R.html)
