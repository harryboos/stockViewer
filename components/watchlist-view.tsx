import { useMemo } from 'react';

import { marketLabel, shortTradeDate } from '@/lib/format';
import type { WatchlistStock } from '@/lib/types';


const MICRO_CHART = [30, 42, 36, 58, 51, 72, 85, 78];

type WatchlistViewProps = {
  today: string;
  stocks: WatchlistStock[];
  query: string;
  loading: boolean;
  missingAiCount: number;
  completedAiCount: number;
  onQueryChange: (value: string) => void;
  onRefresh: () => void;
  onOpenAdd: () => void;
  onOpenStrategies: () => void;
  onRemove: (stock: WatchlistStock) => void;
};

export function WatchlistView({
  today,
  stocks,
  query,
  loading,
  missingAiCount,
  completedAiCount,
  onQueryChange,
  onRefresh,
  onOpenAdd,
  onOpenStrategies,
  onRemove,
}: WatchlistViewProps) {
  const filteredStocks = useMemo(
    () => stocks.filter((stock) => (
      `${stock.name}${stock.symbol}${stock.industry ?? ''}`.toLowerCase().includes(query.toLowerCase())
    )),
    [query, stocks],
  );
  const quotedStocks = stocks.filter((stock) => stock.quote);
  const averageChange = quotedStocks.length
    ? quotedStocks.reduce((sum, stock) => sum + (stock.quote?.pctChg ?? 0), 0) / quotedStocks.length
    : null;
  const upCount = quotedStocks.filter((stock) => (stock.quote?.pctChg ?? 0) >= 0).length;
  const tradeDate = quotedStocks[0]?.quote?.tradeDate ?? null;
  const industryCount = new Set(stocks.map((stock) => stock.industry).filter(Boolean)).size;

  return (
    <section className="content page-enter">
      {!loading && missingAiCount > 0 && (
        <aside className="setup-banner">
          <span className="setup-icon">钥</span>
          <div>
            <strong>免费 A 股行情已启用</strong>
            <p>自选股和公开策略不需要密钥；如需 AI 选股，再在 <code>.env.local</code> 填写对应模型密钥。</p>
          </div>
          <span className="setup-count">{3 - missingAiCount}/3 AI 已配置</span>
        </aside>
      )}

      <div className="hero-row">
        <div>
          <p className="eyebrow">我的投资清单</p>
          <h1>看看今天的自选股</h1>
          <p className="subtitle">{today} · 共关注 {stocks.length} 只股票</p>
        </div>
        <div className="hero-actions">
          <button className="refresh-button" onClick={onRefresh} disabled={loading}>{loading ? '更新中…' : '↻ 更新行情'}</button>
          <button className="add-button" onClick={onOpenAdd}><span>＋</span> 添加股票</button>
        </div>
      </div>

      <div className="summary-grid">
        <article className="summary-card dark-card">
          <div className="card-topline"><span>自选组合今日均值</span><span className="status-pill">{tradeDate ? shortTradeDate(tradeDate) : '无行情'}</span></div>
          <strong className="metric-large">{averageChange === null ? '—' : `${averageChange >= 0 ? '+' : ''}${averageChange.toFixed(2)}%`}</strong>
          <div className="micro-chart" aria-hidden="true">{MICRO_CHART.map((height) => <i key={height} style={{ height: `${height}%` }} />)}</div>
        </article>
        <article className="summary-card">
          <span className="card-label">上涨 / 下跌</span>
          <strong className="metric"><b>{upCount}</b><small> / {Math.max(quotedStocks.length - upCount, 0)}</small></strong>
          <p className="card-note positive">按最新交易日收盘</p>
        </article>
        <article className="summary-card">
          <span className="card-label">关注行业</span>
          <strong className="metric">{industryCount}<small> 个</small></strong>
          <p className="card-note">持久保存在本地 SQLite</p>
        </article>
        <button className="summary-card accent-card signal-card" onClick={onOpenStrategies}>
          <span className="card-label">每日策略 <i>→</i></span>
          <strong className="metric"><b>{completedAiCount}</b><small> / 3</small></strong>
          <p className="card-note">首次打开策略页时自动运行</p>
        </button>
      </div>

      <section className="panel">
        <div className="panel-heading">
          <div><h2>自选股</h2><p>盘中行情快照，缓存 15 分钟；异常时切换备用线路，再自动读取最近日线</p></div>
          <label className="search-box">
            <span>⌕</span>
            <input value={query} onChange={(event) => onQueryChange(event.target.value)} aria-label="搜索自选股" placeholder="搜索代码或名称" />
          </label>
        </div>
        {loading && !stocks.length ? (
          <div className="empty-state"><span className="loading-ring" /><strong>正在读取自选股</strong></div>
        ) : filteredStocks.length ? (
          <div className="stock-table" role="table" aria-label="自选股列表">
            <div className="stock-row table-head" role="row"><span>股票</span><span>收盘价</span><span>涨跌幅</span><span>行业</span><span>交易日</span><span /></div>
            {filteredStocks.map((stock) => (
              <div className="stock-row" role="row" key={stock.tsCode}>
                <div className="stock-name">
                  <span className="market-tag">{marketLabel(stock.exchange)}</span>
                  <div><strong>{stock.name}</strong><small>{stock.symbol}</small></div>
                </div>
                <strong className="price">{stock.quote ? stock.quote.close.toLocaleString('zh-CN', { minimumFractionDigits: 2 }) : '—'}</strong>
                <span className={`change ${(stock.quote?.pctChg ?? 0) >= 0 ? 'up' : 'down'}`}>
                  {stock.quote?.pctChg === null || stock.quote?.pctChg === undefined
                    ? '待更新'
                    : `${stock.quote.pctChg >= 0 ? '+' : ''}${stock.quote.pctChg.toFixed(2)}%`}
                </span>
                <span className="industry">{stock.industry ?? '未分类'}</span>
                <span className="trade-date-cell">{shortTradeDate(stock.quote?.tradeDate)}</span>
                <button className="remove-button" onClick={() => onRemove(stock)} aria-label={`移除${stock.name}`}>×</button>
              </div>
            ))}
          </div>
        ) : (
          <div className="empty-state"><span>⌕</span><strong>没有找到匹配的股票</strong><p>换一个名称或股票代码试试</p></div>
        )}
      </section>
      <p className="data-note">实时行情优先使用东方财富备用线路，并保留 AKShare 与 BaoStock 降级；免费源不提供可用性承诺，请在交易前向券商核对。内容仅供研究，不构成投资建议。</p>
    </section>
  );
}
