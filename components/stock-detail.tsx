'use client';

import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from 'react';
import { useResearch } from '@/lib/use-research';
import { shortTradeDate } from '@/lib/format';
import type { Candle, StockNews, StockProfile, StockSeries } from '@/lib/research-types';
import { percent, timestamp } from '@/components/concept-recommendations';
import { ResearchError } from './research-common';

const StockContext = createContext<(code: string) => void>(() => {});

export function StockLink({ code, children }: { code: string; children: ReactNode }) {
  const open = useContext(StockContext);
  return <button className="stock-detail-link" onClick={() => open(code)} aria-label={`查看 ${code} 个股详情`}>{children}</button>;
}

export function CandleChart({ rows }: { rows: Candle[] }) {
  const bars = rows.slice(-60).filter(row => row.open != null && row.high != null && row.low != null);
  if (!bars.length) return <p className="concept-ai-state">尚无可展示的完整日线。</p>;
  const low = Math.min(...bars.map(row => row.low!));
  const high = Math.max(...bars.map(row => row.high!));
  const range = high - low || high * 0.02 || 1;
  const y = (value: number) => 25 + (high - value) / range * 180;
  const width = 720 / bars.length;
  const maxVolume = Math.max(1, ...bars.map(row => row.vol ?? 0));
  return <figure className="stock-candles" tabIndex={0} aria-label="K线与成交量图，可横向滚动"><svg viewBox="0 0 800 310" role="img" aria-label="最近六十个交易日日K线与成交量">
    {[0, 1, 2, 3].map(index => <g key={index}><line x1="55" x2="780" y1={25 + index * 60} y2={25 + index * 60} stroke="#dbe6df" /><text x="2" y={29 + index * 60}>{(high - range * index / 3).toFixed(2)}</text></g>)}
    {bars.map((row, index) => { const x = 60 + index * width; const color = row.close >= row.open! ? '#cc4b37' : '#20805b';
      return <g key={row.date}><title>{`${row.date} 开 ${row.open} 高 ${row.high} 低 ${row.low} 收 ${row.close} 成交量 ${row.vol ?? '缺失'} 股`}</title><line x1={x} x2={x} y1={y(row.high!)} y2={y(row.low!)} stroke={color} />
        <rect x={x - width * .3} y={Math.min(y(row.open!), y(row.close))} width={width * .6} height={Math.max(1, Math.abs(y(row.open!) - y(row.close)))} fill={color} />
        <rect x={x - width * .3} y={280 - (row.vol ?? 0) / maxVolume * 55} width={width * .6} height={(row.vol ?? 0) / maxVolume * 55} fill={color} opacity=".6" /></g>; })}
    <text x="55" y="303">{bars[0].date}</text><text x="690" y="303">{bars.at(-1)?.date}</text><text x="2" y="248">成交量</text>
  </svg><figcaption>前复权日线 · 上方价格，下方成交量（股）；可横向滚动，悬停查看单日数据</figcaption></figure>;
}

function Detail({ code, close }: { code: string; close: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const profile = useResearch<StockProfile>(`/api/research/stock?code=${code}`);
  const series = useResearch<StockSeries>(`/api/research/stock?code=${code}&part=history`);
  const news = useResearch<StockNews>(`/api/research/stock?code=${code}&part=news`);
  useEffect(() => {
    const prior = document.activeElement as HTMLElement | null;
    dialog.current?.showModal();
    return () => prior?.focus();
  }, []);
  return <dialog ref={dialog} className="stock-detail-dialog" onCancel={event => { event.preventDefault(); close(); }} onClick={event => { if (event.target === event.currentTarget) close(); }} aria-labelledby="stock-detail-title">
    <div className="stock-detail-inner"><header className="research-heading"><div><p className="eyebrow">个股研究</p><h2 id="stock-detail-title">{profile.data?.stock.name || code} <small>{code}</small></h2></div><button className="refresh-button" onClick={close} autoFocus>关闭</button></header>
      <ResearchError error={profile.error} retry={profile.refresh} />
      {profile.data?.quote && <div className="research-metrics"><strong>{profile.data.quote.close.toFixed(2)}</strong><strong className={(profile.data.quote.pctChg ?? 0) >= 0 ? 'up-text' : 'down-text'}>{percent(profile.data.quote.pctChg)}</strong><span>{profile.data.stock.industry || '行业待补'} · 行情 {shortTradeDate(profile.data.quote.tradeDate)} · {profile.data.quote.source}</span></div>}
      <section><h3>走势与成交量</h3><ResearchError error={series.error} retry={series.refresh} />{series.data ? <><CandleChart rows={series.data.rows} /><p className="data-note">{series.data.source} · 读取于 {timestamp(series.data.asOf)}{series.data.warning && ` · ${series.data.warning}`}</p></> : !series.error && <p className="concept-ai-state">正在按需读取日线…</p>}</section>
      <section><h3>关联概念</h3><p className="data-note">{profile.data?.conceptScope}</p><div className="research-chips">{profile.data?.concepts.map(item => <span key={item.code}>{item.name}<small>{timestamp(item.asOf)} · {item.source}</small></span>)}</div>{profile.data && !profile.data.concepts.length && <p>暂未采集到可靠的概念关联，不据名称推断。</p>}</section>
      <section><h3>哪些策略曾选中过它</h3>{profile.data?.selections.length ? <div className="research-records">{profile.data.selections.map((item, index) => <article key={`${item.date}-${item.strategy}-${index}`}><strong>{item.date} · {item.strategy}</strong><p>{item.reason}</p></article>)}</div> : <p>已保存的策略记录中暂无该股票。</p>}</section>
      <section><h3>相关新闻</h3><ResearchError error={news.error} retry={news.refresh} />{news.data?.items.map(item => <article className="research-news" key={item.id}><a href={item.url} target="_blank" rel="noreferrer">{item.title}</a><small>{item.source} · {timestamp(item.publishedAt)}</small><p>{item.excerpt}</p></article>)}{news.data && !news.data.items.length && <p>暂未检索到近期匹配报道。</p>}{!news.data && !news.error && <p>正在读取新闻，其他资料可先查看…</p>}</section>
    </div>
  </dialog>;
}

export function StockDetailProvider({ children }: { children: ReactNode }) {
  const [code, setCode] = useState<string | null>(null);
  return <StockContext.Provider value={setCode}>{children}{code && <Detail key={code} code={code} close={() => setCode(null)} />}</StockContext.Provider>;
}
