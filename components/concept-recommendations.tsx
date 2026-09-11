'use client';

import { useEffect, useRef, useState } from 'react';

import { errorMessage, jsonFetch } from '@/lib/client-api';
import type { ConceptResearch, ConceptRun, ConceptSource } from '@/lib/concept-types';
import { shortTradeDate } from '@/lib/format';

const endpoint = '/api/market/concepts/ai';
const driverLabels = { data: '行情依据', news: '资讯线索', hypothesis: '待验证推测' };

function percent(value: number | null) {
  return value === null ? '—' : `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`;
}

function amount(value: number | null, signed = false) {
  if (value === null) return '—';
  return `${signed && value > 0 ? '+' : ''}${(value / 100_000_000).toFixed(2)} 亿`;
}

function tone(value: number | null) {
  return value === null || value === 0 ? '' : value > 0 ? 'up-text' : 'down-text';
}

function chinaDay() {
  return new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date());
}

function timestamp(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '时间暂缺' : new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(date);
}

function SourceLinks({ sources }: { sources: ConceptSource[] }) {
  return sources.map((source) => (
    <div className="concept-ai-source" key={source.id}>
      <a href={source.url} target="_blank" rel="noopener noreferrer">
        {source.title} ↗<small>{source.source} · {timestamp(source.publishedAt)}</small>
      </a>
      {source.kind === 'news' && <details><summary>查看报道摘录</summary><p>{source.excerpt}</p></details>}
    </div>
  ));
}

export function ConceptResearchCards({ research, today }: { research: ConceptResearch; today: string }) {
  const isToday = shortTradeDate(research.tradeDate) === today;
  const sessionLabel = isToday ? '今日' : '最近交易日';
  return (
    <>
      <div className="concept-ai-summary">
        <p>{research.summary}</p>
        <span>{sessionLabel}行情 · {shortTradeDate(research.tradeDate)}　快照更新 {timestamp(research.dataAsOf)}（北京时间）</span>
      </div>
      <div className="concept-ai-grid">
        {research.concepts.map((concept, index) => (
          <article className="concept-ai-card" key={concept.code}>
            <header className="concept-ai-card-heading">
              <span className="concept-ai-rank">{String(index + 1).padStart(2, '0')}</span>
              <div><small>{concept.strengthStatus === 'recent_strength' ? '近期强势' : '当日活跃观察'} · {concept.code}</small><h3>{concept.name}</h3></div>
              <div className="concept-ai-move"><small>{sessionLabel}</small><strong className={tone(concept.pctChg)}>{percent(concept.pctChg)}</strong></div>
            </header>
            <p className="concept-ai-reason">{concept.reason}</p>
            {concept.strengthStatus === 'today_active' && <p className="concept-ai-trend-note">近期趋势待确认 · 不等同于持续强势</p>}
            <dl className="concept-ai-metrics">
              <div><dt>近 5 日</dt><dd className={tone(concept.change5d)}>{percent(concept.change5d)}</dd></div>
              <div><dt>近 10 日</dt><dd className={tone(concept.change10d)}>{percent(concept.change10d)}</dd></div>
              <div><dt>{sessionLabel}成交额</dt><dd>{amount(concept.amount)}</dd></div>
              <div><dt>主力净流入</dt><dd className={tone(concept.mainNetInflow)}>{amount(concept.mainNetInflow, true)}</dd></div>
              <div><dt>上涨广度</dt><dd>{concept.breadth.toFixed(1)}%</dd></div>
              <div><dt>上涨 / 下跌家数</dt><dd>{concept.upCount} / {concept.downCount}</dd></div>
            </dl>
            {concept.historyAsOf && concept.historyAsOf !== research.tradeDate && <p className="concept-ai-data-note">历史行情截至 {shortTradeDate(concept.historyAsOf)}，缺失的近期涨幅暂不展示。</p>}
            <section className="concept-ai-catalysts" aria-label={`${concept.name}现实催化`}>
              <h4>现实催化 · 为什么走强</h4>
              {concept.catalysts.map((catalyst, catalystIndex) => (
                <div className="concept-ai-catalyst" key={catalystIndex}>
                  <div className="concept-ai-catalyst-title"><span className={`concept-ai-evidence ${catalyst.status === 'reported' ? 'news' : 'hypothesis'}`}>{catalyst.status === 'reported' ? '有报道支持' : '待验证机制'}</span><strong>{catalyst.title}</strong></div>
                  <p>{catalyst.event}</p>
                  <div className="concept-ai-transmission"><small>影响传导</small><p>{catalyst.transmission}</p></div>
                  <p><b>对该概念的意义：</b>{catalyst.impact}</p>
                  <SourceLinks sources={catalyst.sources} />
                </div>
              ))}
            </section>
            <section className="concept-ai-stocks" aria-label={`${concept.name}强势股票`}>
              <h4>强势股票 <span>{sessionLabel}表现</span></h4>
              {concept.stocks.length === 0 && <p className="concept-ai-data-note">暂无可核对的同一交易日强势股行情。</p>}
              {concept.stocks.map((stock) => (
                <div className="concept-ai-stock" key={stock.code}>
                  <div className="concept-ai-stock-quote">
                    <div><strong>{stock.name}</strong><small>{stock.code} · ¥{stock.price.toFixed(2)}</small></div>
                    <b className={tone(stock.pctChg)}>{percent(stock.pctChg)}</b>
                  </div>
                  <span>成交 {amount(stock.amount)} · 换手 {stock.turnoverRate === null ? '—' : `${stock.turnoverRate.toFixed(2)}%`}</span>
                  <p>{stock.reason}</p>
                </div>
              ))}
            </section>
            <section className="concept-ai-drivers" aria-label={`${concept.name}影响因素`}>
              <h4>行情如何验证</h4>
              {concept.drivers.map((driver, driverIndex) => (
                <div className="concept-ai-driver" key={`${driver.kind}-${driverIndex}`}>
                  <div><span className={`concept-ai-evidence ${driver.kind}`}>{driverLabels[driver.kind]}</span><strong>{driver.title}</strong></div>
                  <p>{driver.explanation}</p>
                  <SourceLinks sources={driver.sources} />
                </div>
              ))}
            </section>
            <p className="concept-ai-risk"><strong>观察风险</strong>{concept.risk}</p>
            {concept.warnings.length > 0 && <p className="concept-ai-data-note">{concept.warnings.join('；')}</p>}
          </article>
        ))}
      </div>
      <p className="concept-ai-scope">{research.scope} 近 5/10 日为交易日累计涨幅，缺失项显示“—”。现实催化检索近 30 天新闻，事件报道与股价因果分开判断；盘后新信息不作为前一交易日上涨的原因。仅供研究参考。</p>
      {research.warnings.length > 0 && <p className="concept-ai-data-note">行情提示：{research.warnings.join('；')}</p>}
    </>
  );
}

export function ConceptRecommendations() {
  const [run, setRun] = useState<ConceptRun | null>(null);
  const [requestError, setRequestError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [revision, setRevision] = useState(0);
  const [today, setToday] = useState(chinaDay);
  const submittingRef = useRef(false);
  const requestRef = useRef<AbortController | null>(null);

  useEffect(() => {
    const timer = window.setInterval(() => setToday(chinaDay()), 60_000);
    return () => { window.clearInterval(timer); requestRef.current?.abort(); };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function read() {
      try {
        const next = await jsonFetch<ConceptRun>(endpoint, { signal: controller.signal, cache: 'no-store' });
        if (controller.signal.aborted) return;
        setRun(next);
        setRequestError(null);
        if (next.status === 'running') timer = setTimeout(read, 3000);
      } catch (error) {
        if (controller.signal.aborted) return;
        setRequestError(errorMessage(error, '推荐读取失败'));
        timer = setTimeout(read, 10_000);
      }
    }
    void read();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [revision, today]);

  const current = run?.runDate === today ? run : null;
  const result = current?.status === 'succeeded' ? current.result : null;
  const busy = submitting || current?.status === 'running';

  async function generate() {
    if (submittingRef.current || busy) return;
    const force = Boolean(result);
    if (force && !window.confirm('重新分析会更新今天的推荐，并再次调用 AI。继续吗？')) return;
    submittingRef.current = true;
    setSubmitting(true);
    setRequestError(null);
    const controller = new AbortController();
    requestRef.current = controller;
    try {
      const next = await jsonFetch<ConceptRun>(`${endpoint}${force ? '?force=true' : ''}`, {
        method: 'POST', signal: controller.signal,
      });
      if (!controller.signal.aborted) setRun(next);
    } catch (error) {
      if (!controller.signal.aborted) setRequestError(errorMessage(error, '推荐生成失败'));
    } finally {
      submittingRef.current = false;
      if (!controller.signal.aborted) { setSubmitting(false); setRevision((value) => value + 1); }
    }
  }

  return (
    <section className="concept-ai-section" aria-labelledby="concept-ai-title" aria-busy={busy}>
      <div className="concept-ai-heading">
        <div><p className="eyebrow">AI 概念观察</p><h2 id="concept-ai-title">近期强势概念推荐</h2><p>把近期趋势、当日强势股与背后影响因素放在一起看。</p></div>
        <button className="concept-ai-generate" onClick={generate} disabled={busy || !current || current.status === 'not_configured'}>
          {busy ? '正在分析…' : result ? '重新分析今天' : '生成当日推荐'}
        </button>
      </div>
      <div aria-live="polite">
        {!current && !requestError && <p className="concept-ai-state">正在读取当天推荐…</p>}
        {current?.status === 'idle' && !submitting && <div className="concept-ai-state"><strong>发现近期走强的方向与现实原因</strong><p>点击生成，GLM 5.3 MAX 将比较可用的近 5/10 日趋势与当日行情，检索近 30 天产业、政策、天气和供需事件，选出最多 3 个概念。当天结果会保存供再次查看。</p></div>}
        {current?.status === 'not_configured' && <div className="concept-ai-state"><strong>尚未配置 GLM 5.3 MAX</strong><p>请在服务配置中填写 GLM 的密钥，重启服务后即可生成推荐。</p><button className="refresh-button" onClick={() => setRevision((value) => value + 1)}>重新检查</button></div>}
        {busy && <div className="concept-ai-state concept-ai-progress"><span className="loading-ring" /><div><strong>正在进行 MAX 深度分析</strong><p>正在检索相关新闻并分析影响传导，可能需要数分钟，最多等待 10 分钟。可以先查看下方板块榜单，返回此页会继续读取结果。</p></div></div>}
        {current?.status === 'failed' && <p className="concept-ai-error" role="alert">{current.error || '分析未完成，请重新生成。'}</p>}
        {requestError && <p className="concept-ai-error" role="alert">{requestError} <button onClick={() => setRevision((value) => value + 1)}>重新读取</button></p>}
      </div>
      {result && <ConceptResearchCards research={result} today={today} />}
      <p className="concept-ai-provider">使用 GLM 5.3 MAX 深度分析{current?.finishedAt ? ` · 完成于 ${timestamp(current.finishedAt)}` : ''} · 点击生成才会调用 AI 与联网检索</p>
    </section>
  );
}
