import { shortTradeDate } from '@/lib/format';
import type { MarketOverview } from '@/lib/types';


type MarketOverviewViewProps = {
  today: string;
  data: MarketOverview | null;
  loading: boolean;
  onRefresh: () => void;
};

function formatAmount(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  const yi = value / 100_000_000;
  if (Math.abs(yi) >= 10_000) return `${(yi / 10_000).toFixed(2)}万亿`;
  return `${yi.toFixed(Math.abs(yi) >= 100 ? 0 : 1)}亿`;
}

function formatSignedAmount(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  return `${value >= 0 ? '+' : ''}${formatAmount(value)}`;
}

function formatPct(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  return `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`;
}

function compactDate(value: string): string {
  return value.length === 8 ? `${value.slice(4, 6)}-${value.slice(6, 8)}` : value;
}

function marketTemperature(breadth: number): string {
  if (breadth >= 60) return '扩散偏强';
  if (breadth >= 52) return '温和偏强';
  if (breadth >= 48) return '多空均衡';
  if (breadth >= 40) return '温和偏弱';
  return '扩散偏弱';
}

function friendlyWarnings(warnings: string[]): string[] {
  return [...new Set(warnings.map((warning) => {
    if (warning.startsWith('量能历史暂用')) return '量能历史已使用最近成功缓存';
    if (warning.startsWith('量能历史暂不可用')) return '量能历史暂不可用';
    if (warning.startsWith('实时量能同比暂不可用')) return '实时量能同比暂不可用';
    if (warning.startsWith('资金流暂用')) return '资金流已使用最近成功缓存';
    if (warning.startsWith('资金流暂不可用')) return '资金流暂不可用';
    return '部分扩展数据暂不可用';
  }))];
}

export function MarketOverviewView({ today, data, loading, onRefresh }: MarketOverviewViewProps) {
  if (!data && loading) {
    return (
      <section className="content market-page page-enter">
        <div className="market-loading"><span className="loading-ring" /><strong>正在汇总全市场行情</strong><p>计算量能、涨跌家数和大盘资金流</p></div>
      </section>
    );
  }

  if (!data) {
    return (
      <section className="content market-page page-enter">
        <div className="market-loading"><strong>大盘数据暂未加载</strong><button className="refresh-button" onClick={onRefresh}>重新加载</button></div>
      </section>
    );
  }

  const { snapshot, latestFlow } = data;
  const turnoverMax = Math.max(...data.turnoverHistory.map((item) => item.turnover), 1);
  const flowMax = Math.max(...data.fundFlowHistory.map((item) => Math.abs(item.mainNetInflow)), 1);
  const flowPositive = (latestFlow?.mainNetInflow ?? 0) >= 0;
  const breadthPositive = snapshot.breadth >= 50;
  const warnings = friendlyWarnings(data.warnings);

  return (
    <section className="content market-page page-enter">
      <div className="market-hero">
        <div>
          <p className="eyebrow">全市场脉搏</p>
          <h1>今天的大盘，量能与资金在说什么</h1>
          <p className="subtitle">{today} · {shortTradeDate(data.tradeDate)} · {data.source}</p>
        </div>
        <button className="refresh-button market-refresh" onClick={onRefresh} disabled={loading}>{loading ? '更新中…' : '↻ 更新大盘'}</button>
      </div>

      <div className="market-kpi-grid">
        <article className="market-kpi market-kpi-dark">
          <span className="market-kpi-icon">量</span>
          <div><small>A 股成交额</small><strong>{formatAmount(snapshot.turnover)}</strong><p>全市场最新快照汇总</p></div>
        </article>
        <article className="market-kpi">
          <span className="market-kpi-icon">增</span>
          <div><small>实时量能同比</small><strong className={(snapshot.turnoverDelta ?? 0) >= 0 ? 'up-text' : 'down-text'}>{formatSignedAmount(snapshot.turnoverDelta)}</strong><p>{formatPct(snapshot.turnoverDeltaPct)} · {snapshot.turnoverComparisonDate && snapshot.turnoverComparisonTime ? `对比 ${shortTradeDate(snapshot.turnoverComparisonDate)} ${snapshot.turnoverComparisonTime}` : '同期数据暂不可用'}</p></div>
        </article>
        <article className="market-kpi">
          <span className="market-kpi-icon">资</span>
          <div><small>主力净流入</small><strong className={flowPositive ? 'up-text' : 'down-text'}>{formatSignedAmount(latestFlow?.mainNetInflow)}</strong><p>{latestFlow ? `${shortTradeDate(latestFlow.date)} · 净占比 ${formatPct(latestFlow.mainNetInflowRatio)}` : '资金流历史暂不可用'}</p></div>
        </article>
        <article className="market-kpi">
          <span className="market-kpi-icon">温</span>
          <div><small>市场温度</small><strong className={breadthPositive ? 'up-text' : 'down-text'}>{marketTemperature(snapshot.breadth)}</strong><p>上涨家数占比 {snapshot.breadth.toFixed(1)}%</p></div>
        </article>
      </div>

      <div className="market-primary-grid">
        <article className="market-panel market-flow-panel">
          <div className="market-panel-heading"><div><small>资金趋势</small><h2>近 {data.fundFlowHistory.length || '—'} 日主力净流入</h2></div><span>单位：亿元</span></div>
          {data.fundFlowHistory.length ? (
            <div className="fund-flow-chart" role="img" aria-label="近期主力资金净流入柱状图">
              {data.fundFlowHistory.map((item) => {
                const height = `${Math.max(Math.abs(item.mainNetInflow) / flowMax * 45, 3)}%`;
                return (
                  <div className="fund-flow-column" key={item.date} title={`${shortTradeDate(item.date)} ${formatSignedAmount(item.mainNetInflow)}`}>
                    <div className="flow-bar-space"><i className={item.mainNetInflow >= 0 ? 'positive' : 'negative'} style={{ height }} /></div>
                    <small>{compactDate(item.date)}</small>
                  </div>
                );
              })}
            </div>
          ) : <div className="market-chart-empty">资金流上游暂不可用，当前行情快照仍可正常查看。</div>}
        </article>

        <article className="market-panel market-breadth-panel">
          <div className="market-panel-heading"><div><small>涨跌广度</small><h2>市场赚钱效应</h2></div><span>{snapshot.advancers + snapshot.decliners + snapshot.flat} 只</span></div>
          <div className="breadth-visual">
            <div className="breadth-ring" style={{ background: `conic-gradient(#c8503d 0 ${snapshot.breadth}%, #27825a ${snapshot.breadth}% 100%)` }}><span><strong>{snapshot.breadth.toFixed(0)}%</strong><small>上涨占比</small></span></div>
            <div className="breadth-stats">
              <div><span className="breadth-dot up" /><small>上涨</small><strong>{snapshot.advancers}</strong></div>
              <div><span className="breadth-dot flat" /><small>平盘</small><strong>{snapshot.flat}</strong></div>
              <div><span className="breadth-dot down" /><small>下跌</small><strong>{snapshot.decliners}</strong></div>
            </div>
          </div>
          <div className="limit-row"><span>涨停约 <b className="up-text">{snapshot.limitUp}</b> 家</span><span>跌停约 <b className="down-text">{snapshot.limitDown}</b> 家</span><span>中位涨幅 <b>{formatPct(snapshot.medianPctChg)}</b></span></div>
        </article>
      </div>

      <div className="market-secondary-grid">
        <article className="market-panel turnover-panel">
          <div className="market-panel-heading"><div><small>量能趋势</small><h2>近 {data.turnoverHistory.length} 个交易日成交额</h2></div><span>沪深历史 + 今日快照</span></div>
          <div className="turnover-chart" role="img" aria-label="近期市场成交额柱状图">
            {data.turnoverHistory.map((item) => (
              <div className="turnover-column" key={item.date} title={`${shortTradeDate(item.date)} ${formatAmount(item.turnover)}`}>
                <i style={{ height: `${Math.max(item.turnover / turnoverMax * 100, 5)}%` }} />
                <small>{compactDate(item.date)}</small>
              </div>
            ))}
          </div>
        </article>

        <article className="market-panel focus-panel">
          <div className="market-panel-heading"><div><small>量能焦点</small><h2>成交额前列</h2></div><span>最新快照</span></div>
          <div className="turnover-focus-list">
            {data.topTurnover.map((item, index) => (
              <div key={item.code}><span>{index + 1}</span><div><strong>{item.name}</strong><small>{item.code}</small></div><b className={(item.pctChg ?? 0) >= 0 ? 'up-text' : 'down-text'}>{formatPct(item.pctChg)}</b><em>{formatAmount(item.amount)}</em></div>
            ))}
          </div>
        </article>
      </div>

      <div className="market-footnote">
        <p>主力资金流来自东方财富历史接口；A 股成交额为全市场行情快照汇总。实时量能同比使用沪深两市一致口径，盘中对比前一交易日同一时点的累计成交额，收盘后对比完整交易日；涨跌停数量按不同板块常用阈值估算。</p>
        {warnings.length > 0 && <p className="market-warning">部分扩展数据已降级：{warnings.join('；')}</p>}
      </div>
    </section>
  );
}
