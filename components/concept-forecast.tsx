'use client';
import { StockLink } from './stock-detail';

import { SourceLinks, amount, percent, timestamp, tone } from '@/components/concept-recommendations';
import type { ForecastResearch } from '@/lib/forecast-types';
import { shortTradeDate } from '@/lib/format';
import { useDailyConceptRun } from '@/lib/use-daily-concept-run';
import { ForecastHistory } from '@/components/forecast-history';

const number = (value: number | null) => value === null ? '—' : value.toFixed(2);
const perspectives = [['technical', '技术面'], ['fundamental', '基本面'], ['news', '时事新闻']] as const;

export function ForecastResearchCards({ research, today }: { research: ForecastResearch; today: string }) {
  const sessionLabel = shortTradeDate(research.tradeDate) === today ? '今日' : '最近交易日';
  return (
    <>
      <div className="forecast-window">
        <span>预测区间 <strong>{research.window.startDate} — {research.window.endDate}</strong></span>
        <span>{research.window.calendarDays} 个自然日 · 含休市日</span>
      </div>
      <div className="concept-ai-summary">
        <p>{research.summary}</p>
        <span>{sessionLabel}行情 · {shortTradeDate(research.tradeDate)}　快照更新 {timestamp(research.dataAsOf)}（北京时间）</span>
      </div>
      {research.feedbackUsed?.asOf && <p className="concept-ai-data-note">本次参考的已到期反馈：半个月 {research.feedbackUsed.sampleCounts['15'] ?? 0} 个样本 · 一个月 {research.feedbackUsed.sampleCounts['30'] ?? 0} 个样本。仅使用生成前已核对的结果。</p>}
      {research.concepts.length === 0 && <p className="concept-ai-state">当前证据不足以选出预测方向，暂不强行给出名单。可在行情或新闻更新后重新分析。</p>}
      <div className="forecast-results">
        {research.concepts.map((concept, index) => (
          <article className="concept-ai-card forecast-card" key={concept.code}>
            <header className="concept-ai-card-heading">
              <span className="concept-ai-rank">{String(index + 1).padStart(2, '0')}</span>
              <div><small>潜在强势方向 · {concept.code}</small><h3>{concept.name}</h3></div>
              <span className={`forecast-conviction ${concept.conviction}`}>研究把握度：{concept.conviction === 'medium' ? '中等' : '较低'}</span>
              <div className="concept-ai-move"><small>{sessionLabel}</small><strong className={tone(concept.pctChg)}>{percent(concept.pctChg)}</strong></div>
            </header>
            <p className="forecast-thesis">{concept.thesis}</p>
            <dl className="concept-ai-metrics forecast-metrics">
              <div><dt>近 5 日涨幅</dt><dd className={tone(concept.change5d)}>{percent(concept.change5d)}</dd></div>
              <div><dt>近 10 日涨幅</dt><dd className={tone(concept.change10d)}>{percent(concept.change10d)}</dd></div>
              <div><dt>{sessionLabel}成交额</dt><dd>{amount(concept.amount)}</dd></div>
              <div><dt>主力净流入</dt><dd className={tone(concept.mainNetInflow)}>{amount(concept.mainNetInflow, true)}</dd></div>
              <div><dt>上涨广度</dt><dd>{concept.breadth.toFixed(1)}%</dd></div>
              <div><dt>上涨 / 下跌家数</dt><dd>{concept.upCount} / {concept.downCount}</dd></div>
            </dl>
            <div className="forecast-perspectives">
              {perspectives.map(([key, label]) => (
                <section className="forecast-perspective" aria-label={`${concept.name}${label}预测依据`} key={key}>
                  <h4>{label}<span className={`concept-ai-evidence ${concept[key].status === 'supported' ? 'data' : 'hypothesis'}`}>{concept[key].status === 'supported' ? '有材料支持' : '待验证'}</span></h4>
                  <p>{concept[key].summary}</p>
                  {key === 'technical' && <>
                    <dl className="forecast-technical">
                      <div><dt>指数最新值</dt><dd>{number(concept.technicalData.lastClose)}</dd></div>
                      <div><dt>5 / 10 / 20 日均线</dt><dd>{number(concept.technicalData.ma5)} / {number(concept.technicalData.ma10)} / {number(concept.technicalData.ma20)}</dd></div>
                      <div><dt>RSI（14 日）</dt><dd>{number(concept.technicalData.rsi14)}</dd></div>
                      <div><dt>距 20 日最高收盘价</dt><dd>{percent(concept.technicalData.distanceTo20dHigh)}</dd></div>
                      <div><dt>完整 5 日成交额比</dt><dd>{number(concept.technicalData.amountRatio5d)}{concept.technicalData.amountRatio5d === null ? '' : ' 倍'}</dd></div>
                    </dl>
                    <p className="concept-ai-data-note">成交额比剔除行情当日，比较最近 5 个完整交易日与此前 5 日。历史截至 {concept.technicalData.historyAsOf ? shortTradeDate(concept.technicalData.historyAsOf) : '暂缺'}。</p>
                  </>}
                  {key === 'fundamental' && <p className="concept-ai-data-note">{concept.fundamentalData.note}</p>}
                  <SourceLinks sources={concept[key].sources} />
                </section>
              ))}
            </div>
            <div className="forecast-detail-grid">
              <section className="concept-ai-catalysts" aria-label={`${concept.name}现实影响因素`}>
                <h4>现实影响因素 · 为什么可能走强</h4>
                {concept.catalysts.map((catalyst, i) => (
                  <div className="concept-ai-catalyst" key={i}>
                    <div className="concept-ai-catalyst-title"><span className={`concept-ai-evidence ${catalyst.status === 'reported' ? 'news' : 'hypothesis'}`}>{catalyst.status === 'reported' ? '有报道支持' : '待验证机制'}</span><strong>{catalyst.title}</strong></div>
                    <p>{catalyst.event}</p>
                    <div className="concept-ai-transmission"><small>事件如何传导到产业与盈利</small><p>{catalyst.transmission}</p></div>
                    <p><b>未来半个月的影响：</b>{catalyst.impact}</p>
                    <SourceLinks sources={catalyst.sources} />
                  </div>
                ))}
              </section>
              <section className="concept-ai-stocks" aria-label={`${concept.name}强势股票`}>
                <h4>强势股票 <span>{sessionLabel}表现</span></h4>
                {concept.stocks.length === 0 && <p className="concept-ai-data-note">暂无可核对的同一交易日强势股行情。</p>}
                {concept.stocks.map((stock) => {
                  const valuation = concept.fundamentalData.valuations.find((row) => row.code === stock.code);
                  return (
                    <div className="concept-ai-stock" key={stock.code}>
                      <div className="concept-ai-stock-quote"><div><StockLink code={stock.code}><strong>{stock.name}</strong></StockLink><small>{stock.code} · ¥{stock.price.toFixed(2)}</small></div><b className={tone(stock.pctChg)}>{percent(stock.pctChg)}</b></div>
                      <span>成交 {amount(stock.amount)} · 换手 {number(stock.turnoverRate)}{stock.turnoverRate === null ? '' : '%'}</span>
                      <span>动态 PE {number(valuation?.peDynamic ?? null)} · PB {number(valuation?.pb ?? null)} · 市值 {amount(valuation?.marketCap ?? null)}</span>
                      <p>{stock.reason}</p>
                    </div>
                  );
                })}
                <p className="concept-ai-data-note">当前强势股不代表未来必然领涨；估值为已取得成份股的快照。</p>
              </section>
            </div>
            <div className="forecast-conditions">
              <div><h4>什么出现，判断更成立</h4><p>{concept.confirmation}</p></div>
              <div><h4>什么出现，预测应失效</h4><p>{concept.invalidation}</p></div>
            </div>
            {concept.warnings.length > 0 && <p className="concept-ai-data-note">{concept.warnings.join('；')}</p>}
          </article>
        ))}
      </div>
      <p className="concept-ai-scope">{research.scope} 研究把握度不是统计胜率；以上为基于当前资料的条件推演，不保证收益。新闻提供事实线索，未来影响仍需验证，缺失数据以“—”展示。</p>
      {research.warnings.length > 0 && <p className="concept-ai-data-note">行情提示：{research.warnings.join('；')}</p>}
    </>
  );
}

export function ConceptForecast() {
  const { current, result, busy, submitting, requestError, today, generate, refresh } = useDailyConceptRun<ForecastResearch>('/api/forecast/concepts', '预测');
  return (
    <div className="content forecast-content">
      <section className="concept-ai-section forecast-section" aria-labelledby="concept-forecast-title" aria-busy={busy}>
        <div className="concept-ai-heading">
          <div><p className="eyebrow">AI 概念预测</p><h2 id="concept-forecast-title">未来半个月强势概念预测</h2><p>结合技术面、基本面与时事新闻，推演未来 15 个自然日的机会与失效条件。</p></div>
          <button className="concept-ai-generate" onClick={generate} disabled={busy || !current || current.status === 'not_configured'}>{busy ? '正在预测…' : result ? '重新生成预测' : '生成半个月预测'}</button>
        </div>
        <div aria-live="polite">
          {!current && !requestError && <p className="concept-ai-state">正在读取当天预测…</p>}
          {current?.status === 'idle' && !submitting && <div className="concept-ai-state"><strong>寻找接下来可能走强的方向</strong><p>GLM 5.3 MAX 将比较近期走势与估值线索，检索产业、政策、供需等新闻，选出最多 3 个概念及相关强势股。预测从生成日次日起算，当天结果会保存。</p></div>}
          {current?.status === 'not_configured' && <div className="concept-ai-state"><strong>尚未配置 GLM 5.3 MAX</strong><p>请在服务配置中填写 GLM 的密钥，重启服务后即可生成预测。</p><button className="refresh-button" onClick={refresh}>重新检查</button></div>}
          {busy && <div className="concept-ai-state concept-ai-progress"><span className="loading-ring" /><div><strong>正在结合三方面进行 MAX 深度预测</strong><p>正在收集行情与现实事件，推演未来半个月的影响。高峰期遇到临时限流会自动等待并重试，总等待不超过 10 分钟。可以切换其他 Tab，返回后继续查看结果。</p></div></div>}
          {current?.status === 'failed' && <p className="concept-ai-error" role="alert">{current.error || '预测未完成，请重新生成。'}</p>}
          {requestError && <p className="concept-ai-error" role="alert">{requestError} <button onClick={refresh}>重新读取</button></p>}
        </div>
        {current?.status !== 'succeeded' && result && <p className="data-note">以下保留上次成功结果 · {current?.previousFinishedAt ? timestamp(current.previousFinishedAt) : '完成时间待补'}</p>}
        {result && <ForecastResearchCards research={result} today={today} />}
        <p className="concept-ai-provider">使用 GLM 5.3 MAX 深度分析{current?.finishedAt ? ` · 完成于 ${timestamp(current.finishedAt)}` : ''} · 点击生成才会调用 AI 与联网检索</p>
      </section>
      <ForecastHistory finishedAt={current?.finishedAt} renderResearch={research => <ForecastResearchCards research={research} today={today} />} />
    </div>
  );
}
