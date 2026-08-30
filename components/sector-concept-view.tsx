'use client';

import { useState } from 'react';

import { shortTradeDate } from '@/lib/format';
import type { SectorBoard, SectorOverview } from '@/lib/types';


type SectorConceptViewProps = {
  today: string;
  data: SectorOverview | null;
  loading: boolean;
  onRefresh: () => void;
};

function formatAmount(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  const yi = value / 100_000_000;
  if (Math.abs(yi) >= 10_000) return `${(yi / 10_000).toFixed(2)}万亿`;
  return `${yi >= 0 ? '+' : ''}${yi.toFixed(Math.abs(yi) >= 100 ? 0 : 1)}亿`;
}

function formatTurnover(value: number | null | undefined): string {
  return formatAmount(value).replace(/^\+/, '');
}

function formatPct(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  return `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`;
}

function changeTone(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '';
  return value >= 0 ? 'up-text' : 'down-text';
}

function BoardRanking({ title, eyebrow, boards }: { title: string; eyebrow: string; boards: SectorBoard[] }) {
  const maxMove = Math.max(...boards.map((board) => Math.abs(board.pctChg)), 1);
  return (
    <article className="market-panel sector-ranking-panel">
      <div className="market-panel-heading">
        <div><small>{eyebrow}</small><h2>{title}</h2></div>
        <span>涨幅 · 成交额 · 较昨日 · 主力资金</span>
      </div>
      {boards.length ? (
        <div className="sector-ranking-list">
          {boards.map((board, index) => (
            <div className="sector-ranking-row" key={board.code}>
              <span className="sector-rank">{index + 1}</span>
              <div className="sector-rank-name">
                <strong>{board.name}</strong>
                <small>{board.code} · 上涨广度 {board.breadth.toFixed(0)}%</small>
                <small className="sector-rank-amount">成交 {formatTurnover(board.amount)} · <i className={changeTone(board.amountDelta)}>较昨 {formatAmount(board.amountDelta)}</i></small>
              </div>
              <div className="sector-strength"><i className={board.pctChg >= 0 ? 'positive' : 'negative'} style={{ width: `${Math.max(Math.abs(board.pctChg) / maxMove * 100, 4)}%` }} /></div>
              <b className={board.pctChg >= 0 ? 'up-text' : 'down-text'}>{formatPct(board.pctChg)}</b>
              <em className={(board.mainNetInflow ?? 0) >= 0 ? 'up-text' : 'down-text'}>{formatAmount(board.mainNetInflow)}</em>
            </div>
          ))}
        </div>
      ) : <div className="sector-empty">当前没有可展示的{eyebrow}数据</div>}
    </article>
  );
}

function LeaderBoardCard({ board, index }: { board: SectorBoard; index: number }) {
  return (
    <article className="sector-leader-card">
      <div className="sector-card-head">
        <span>{String(index + 1).padStart(2, '0')}</span>
        <div><small>{board.kind === 'industry' ? '行业板块' : '概念主题'} · {board.code}</small><h3>{board.name}</h3></div>
        <b className={board.pctChg >= 0 ? 'up-text' : 'down-text'}>{formatPct(board.pctChg)}</b>
      </div>
      <div className="sector-card-turnover">
        <span><small>今日成交额</small><strong>{formatTurnover(board.amount)}</strong></span>
        <span><small>较昨日</small><strong className={changeTone(board.amountDelta)}>{formatAmount(board.amountDelta)}</strong></span>
      </div>
      <div className="sector-card-metrics">
        <span><small>上涨广度</small><strong>{board.breadth.toFixed(0)}%</strong></span>
        <span><small>换手率</small><strong>{formatPct(board.turnoverRate)}</strong></span>
        <span><small>主力净流入</small><strong className={(board.mainNetInflow ?? 0) >= 0 ? 'up-text' : 'down-text'}>{formatAmount(board.mainNetInflow)}</strong></span>
      </div>
      <div className="sector-leaders">
        {board.leaders.length ? board.leaders.map((leader) => (
          <div key={`${leader.role}-${leader.code ?? leader.name}`}>
            <span>{leader.role}</span>
            <div><strong>{leader.name}</strong><small>{leader.code ?? '代码待补充'} · {leader.price === null ? '价格暂缺' : `现价 ${leader.price.toFixed(2)}`}</small></div>
            <b className={(leader.pctChg ?? 0) >= 0 ? 'up-text' : 'down-text'}>{formatPct(leader.pctChg)}</b>
            <em>{leader.amount === null ? '成交额暂缺' : `成交 ${formatAmount(leader.amount).replace('+', '')}`}</em>
          </div>
        )) : <p>上游暂未返回可核对的龙头股行情。</p>}
      </div>
    </article>
  );
}

export function SectorConceptView({ today, data, loading, onRefresh }: SectorConceptViewProps) {
  const [mode, setMode] = useState<'industry' | 'concept'>('industry');

  if (!data && loading) {
    return (
      <section className="content market-page page-enter">
        <div className="market-loading"><span className="loading-ring" /><strong>正在扫描行业与概念板块</strong><p>合并板块强度、成交额、涨跌广度、资金流和龙头股</p></div>
      </section>
    );
  }

  if (!data) {
    return (
      <section className="content market-page page-enter">
        <div className="market-loading"><strong>板块概念数据暂未加载</strong><button className="refresh-button" onClick={onRefresh}>重新加载</button></div>
      </section>
    );
  }

  const leaders = mode === 'industry' ? data.industryBoards : data.conceptBoards;
  const topBoard = data.summary.topBoard;
  const topFundBoard = data.summary.topFundBoard;

  return (
    <section className="content market-page sector-page page-enter">
      <div className="market-hero">
        <div>
          <p className="eyebrow">板块雷达</p>
          <h1>行业与概念，谁在带动今天的行情</h1>
          <p className="subtitle">{today} · {shortTradeDate(data.tradeDate)} · {data.source}</p>
        </div>
        <button className="refresh-button market-refresh" onClick={onRefresh} disabled={loading}>{loading ? '更新中…' : '↻ 更新板块'}</button>
      </div>

      <div className="market-kpi-grid sector-kpi-grid">
        <article className="market-kpi market-kpi-dark"><span className="market-kpi-icon">行</span><div><small>上涨行业</small><strong>{data.summary.risingIndustryCount}<i> / {data.summary.industryCount}</i></strong><p>东方财富行业板块口径</p></div></article>
        <article className="market-kpi"><span className="market-kpi-icon">概</span><div><small>上涨概念</small><strong>{data.summary.risingConceptCount}<i> / {data.summary.conceptCount}</i></strong><p>已过滤连板、重仓等标签型概念</p></div></article>
        <article className="market-kpi"><span className="market-kpi-icon">强</span><div><small>强度冠军</small><strong className="up-text">{topBoard?.name ?? '—'}</strong><p>{topBoard ? `${topBoard.kind === 'industry' ? '行业' : '概念'} · ${formatPct(topBoard.pctChg)}` : '板块强度暂缺'}</p></div></article>
        <article className="market-kpi"><span className="market-kpi-icon">资</span><div><small>资金冠军</small><strong className={(topFundBoard?.mainNetInflow ?? 0) >= 0 ? 'up-text' : 'down-text'}>{topFundBoard?.name ?? '—'}</strong><p>{topFundBoard ? `主力净流入 ${formatAmount(topFundBoard.mainNetInflow)}` : '板块资金流暂缺'}</p></div></article>
      </div>

      <div className="sector-ranking-grid">
        <BoardRanking title="今日行业强度榜" eyebrow="行业轮动" boards={data.industryBoards} />
        <BoardRanking title="今日概念热度榜" eyebrow="主题热度" boards={data.conceptBoards} />
      </div>

      <section className="sector-leader-section">
        <div className="sector-leader-heading">
          <div><p className="eyebrow">龙头矩阵</p><h2>板块强势股</h2><span>领涨龙头看价格强度，资金龙头看主力净流入最大股；两者可能重合。</span></div>
          <div className="sector-mode-switch" role="group" aria-label="切换龙头板块类型">
            <button className={mode === 'industry' ? 'active' : ''} aria-pressed={mode === 'industry'} onClick={() => setMode('industry')}>行业龙头</button>
            <button className={mode === 'concept' ? 'active' : ''} aria-pressed={mode === 'concept'} onClick={() => setMode('concept')}>概念龙头</button>
          </div>
        </div>
        <div className="sector-leader-grid">
          {leaders.map((board, index) => <LeaderBoardCard board={board} index={index} key={board.code} />)}
        </div>
      </section>

      <div className="market-footnote">
        <p>板块强度、今日成交额、上涨家数与领涨股来自东方财富实时板块行情；“较昨日”按前一交易日板块日线成交额计算，主力资金龙头来自板块资金流排名。龙头仅表示当日价格或资金口径靠前，不代表公司基本面质量或后续涨幅。</p>
        {data.warnings.length > 0 && <p className="market-warning">部分扩展数据已降级：{data.warnings.join('；')}</p>}
      </div>
    </section>
  );
}
