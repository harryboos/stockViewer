'use client';

import type { DailyDigestData } from '@/lib/research-types';
import { useResearch } from '@/lib/use-research';
import { percent, timestamp, tone, shortTradeDate as tradeDate } from '@/lib/format';
import { ResearchError } from './research-common';
import { StockLink } from './stock-detail';

type Navigate = (tab: 'watchlist' | 'market' | 'sectors' | 'strategies' | 'forecast') => void;
const turnover = (value?: number | null) => value == null ? '—' : value >= 1e12 ? `${(value / 1e12).toFixed(2)} 万亿` : `${(value / 1e8).toLocaleString('zh-CN', { maximumFractionDigits: 0 })} 亿`;

export function DailyDigest({ navigate }: { navigate: Navigate }) {
  const { data, error, refresh } = useResearch<DailyDigestData>('/api/research/digest', 15000);
  return <DailyDigestContent data={data} error={error} refresh={refresh} navigate={navigate} />;
}

export function DailyDigestContent({ data, error = null, refresh, navigate }: {
  data: DailyDigestData | null; error?: string | null; refresh: () => void; navigate: Navigate;
}) {
  const loading = !data && !error;
  const breadth = data?.market?.snapshot.breadth;
  const empty = (message: string) => <p className="digest-empty">{loading ? '正在读取摘要…' : message}</p>;

  return <div className="content digest-page" aria-busy={loading}>
    <header className="digest-heading">
      <div><p className="eyebrow">你的每日观察台</p><h1>每日摘要</h1><p>先看市场，再看你关注的机会。</p></div>
      <button className="refresh-button" onClick={refresh}><span aria-hidden="true">↻</span> 刷新摘要</button>
    </header>
    <ResearchError error={error} retry={refresh} />

    <section className="digest-market" aria-label="大盘概览">
      <div className="digest-market-intro"><span className="digest-market-label">市场速览</span><strong>{tradeDate(data?.market?.tradeDate)}</strong><span>最近交易日</span><button onClick={() => navigate('market')}>查看大盘 <span aria-hidden="true">↗</span></button></div>
      <div className="digest-market-metric"><span>上涨广度</span><strong>{breadth == null ? '—' : `${breadth.toFixed(1)}%`}</strong><div className="digest-breadth" aria-hidden="true"><i style={{ width: `${Math.max(0, Math.min(100, breadth ?? 0))}%` }} /></div><small>上涨个股占比</small></div>
      <div className="digest-market-metric"><span>全市场成交额</span><strong>{turnover(data?.market?.snapshot.turnover)}</strong><small>{data?.market ? `快照更新 ${timestamp(data.market.updatedAt)}` : '尚未取得大盘快照'}</small></div>
    </section>
    {!!data?.market?.warnings.length && <p className="digest-warning">行情提示：{data.market.warnings.join('；')}</p>}

    <div className="digest-columns">
      <div className="digest-column">
        <section className="digest-card digest-watch">
          <header><div><p className="digest-kicker">MY WATCHLIST</p><h2>自选异动</h2></div><span className="digest-date">{tradeDate(data?.quoteDate)}</span></header>
          <div className="digest-list-caption"><span>按涨跌幅绝对值排序 · 最多 5 只</span><span>当日涨跌</span></div>
          {data?.watchMovers.length ? <ol className="digest-watch-list">{data.watchMovers.map((stock, index) => <li key={stock.tsCode}>
            <span className="digest-rank">{String(index + 1).padStart(2, '0')}</span>
            <div className="digest-stock"><StockLink code={stock.symbol}>{stock.name}</StockLink><small>{stock.symbol}{stock.industry ? ` · ${stock.industry}` : ''}</small></div>
            <strong className={tone(stock.quote?.pctChg ?? null)}>{percent(stock.quote?.pctChg ?? null)}</strong>
          </li>)}</ol> : empty('暂无可比较行情，添加自选股或更新行情后查看。')}
          <footer><button onClick={() => navigate('watchlist')}>查看全部自选 <span aria-hidden="true">→</span></button></footer>
        </section>

        <section className="digest-card digest-strategies">
          <header><div><p className="digest-kicker">STRATEGY PICKS</p><h2>近期策略名单</h2></div><span className="digest-tag">最近 {data?.recentSelections.length ?? '—'} 份</span></header>
          {data?.recentSelections.length ? <div className="digest-strategy-list">{data.recentSelections.map(entry => <article key={entry.id}>
            <div className="digest-strategy-heading"><h3>{entry.name}</h3><time dateTime={entry.runDate}>{tradeDate(entry.runDate)}</time></div>
            <div className="digest-picks">{entry.picks.length ? entry.picks.map(pick => <StockLink key={pick.code} code={pick.code}>{pick.name}</StockLink>) : <span className="digest-no-picks">本次未选出股票</span>}</div>
          </article>)}</div> : empty('暂无策略名单，完成选股后会自动汇总到这里。')}
          <footer><button onClick={() => navigate('strategies')}>查看策略与历史成绩 <span aria-hidden="true">→</span></button></footer>
        </section>
      </div>

      <div className="digest-column">
        <section className="digest-card digest-hotspots">
          <header><div><p className="digest-kicker">MARKET FOCUS</p><h2>概念风向</h2></div><span className="digest-date">{tradeDate(data?.sectorTradeDate)}</span></header>
          {data?.strongBoards.length ? <ol className="digest-board-list">{data.strongBoards.map((board, index) => <li key={board.code}>
            <span className="digest-rank">{String(index + 1).padStart(2, '0')}</span><div><strong>{board.name}</strong><small>{board.code} · 上涨广度 {board.breadth.toFixed(1)}%</small></div><b className={tone(board.pctChg)}>{percent(board.pctChg)}</b>
          </li>)}</ol> : empty('尚无板块快照，更新板块后查看当前热点。')}
          <footer><button onClick={() => navigate('sectors')}>板块与轮动 <span aria-hidden="true">→</span></button><small>{data?.sectorAsOf ? timestamp(data.sectorAsOf) : '等待更新'}</small></footer>
        </section>

        <section className="digest-card digest-feedback">
          <header><div><p className="digest-kicker">PREDICTION REVIEW</p><h2>预测复盘</h2></div><span className="digest-tag">持续跟踪</span></header>
          <div className="digest-horizons">{(['15', '30'] as const).map(days => {
            const summary = data?.forecastSummaries[days];
            const completed = (summary?.sampleCount ?? 0) > 0;
            return <article key={days}>
              <div className="digest-horizon-title"><h3>{days === '15' ? '半个月' : '一个月'}</h3><span>{days === '15' ? '预测评估' : '延伸观察'}</span></div>
              <strong className={`digest-return ${tone(completed ? summary?.averageReturnPct ?? null : null)}`}>{percent(completed ? summary?.averageReturnPct ?? null : null)}</strong>
              <p>{completed ? `${summary?.sampleCount} 个到期样本 · 平均涨幅` : !summary ? '等待读取复盘' : summary.totalCount === 0 ? '尚无预测样本' : summary.missingCount > 0 && summary.trackingCount === 0 ? '到期数据待补全' : '等待样本到期'}</p>
              <div className="digest-feedback-count"><span>跟踪中 <b>{summary?.trackingCount ?? '—'}</b></span><span>到期待补 <b>{summary?.missingCount ?? '—'}</b></span></div>
            </article>;
          })}</div>
          <p className="digest-feedback-note">仅到期且数据完整的样本计入均值。</p>
          <footer><button onClick={() => navigate('forecast')}>查看预测与同期最强对照 <span aria-hidden="true">→</span></button></footer>
        </section>
      </div>
    </div>
    <p className="digest-footnote">{data?.note || '汇总已有行情与研究结果。'}{data?.asOf ? ` · 摘要更新 ${timestamp(data.asOf)}` : ''}</p>
  </div>;
}
