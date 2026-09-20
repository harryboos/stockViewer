import { useEffect, useMemo, useRef, useState } from 'react';

import { marketLabel, shortTradeDate } from '@/lib/format';
import type { WatchlistStock } from '@/lib/types';
import { summarizeWatchlist } from '@/lib/watchlist-summary';
import { StockLink } from './stock-detail';
import { errorMessage } from '@/lib/client-api';
import type { WatchNote } from '@/lib/use-watchlist';


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
  onSaveNote: (note: WatchNote) => Promise<void>;
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
  onSaveNote,
}: WatchlistViewProps) {
  const [group, setGroup] = useState('');
  const [sort, setSort] = useState('added');
  const [editing, setEditing] = useState<WatchlistStock | null>(null);
  const groups = [...new Set(stocks.map(stock => stock.groupName || '未分组'))];
  const filteredStocks = useMemo(
    () => stocks.filter((stock) => (
      `${stock.name}${stock.symbol}${stock.industry ?? ''}`.toLowerCase().includes(query.toLowerCase())
      && (!group || (stock.groupName || '未分组') === group)
    )).sort((a, b) => sort === 'change' ? (b.quote?.pctChg ?? -Infinity) - (a.quote?.pctChg ?? -Infinity)
      : sort === 'name' ? a.name.localeCompare(b.name, 'zh-CN') : (b.addedAt || '').localeCompare(a.addedAt || '')),
    [query, stocks, group, sort],
  );
  const { averageChange, upCount, downCount, flatCount, tradeDate } = summarizeWatchlist(stocks);
  const industryCount = new Set(stocks.map((stock) => stock.industry).filter(Boolean)).size;

  return (
    <section className="content page-enter">


      <div className="hero-row">
        <div>
          <p className="eyebrow">我的投资清单</p>
          <h1>我的自选</h1>
          <p className="subtitle">{today} · 共关注 {stocks.length} 只股票</p>
        </div>
        <div className="hero-actions">
          <button className="refresh-button" onClick={onRefresh} disabled={loading}>{loading ? '更新中…' : '↻ 更新行情'}</button>
          <button className="add-button" onClick={onOpenAdd} disabled={loading}><span>＋</span> 添加股票</button>
        </div>
      </div>

      <div className="summary-grid">
        <article className="summary-card dark-card">
          <div className="card-topline"><span>自选组合今日均值</span><span className="status-pill">{tradeDate ? shortTradeDate(tradeDate) : '无行情'}</span></div>
          <strong className="metric-large">{averageChange === null ? '—' : `${averageChange >= 0 ? '+' : ''}${averageChange.toFixed(2)}%`}</strong>
        </article>
        <article className="summary-card">
          <span className="card-label">上涨 / 下跌</span>
          <strong className="metric"><b>{upCount}</b><small> / {downCount}</small></strong>
          <p className="card-note positive">最新交易日 · 平盘 {flatCount} 只 · 缺失数据不计入</p>
        </article>
        <article className="summary-card">
          <span className="card-label">关注行业</span>
          <strong className="metric">{industryCount}<small> 个</small></strong>
          <p className="card-note">按行业查看关注方向</p>
        </article>
        <button className="summary-card accent-card signal-card" onClick={onOpenStrategies}>
          <span className="card-label">每日策略 <i>→</i></span>
          <strong className="metric"><b>{completedAiCount}</b><small> / 3</small></strong>
          <p className="card-note">浏览只读状态 · 点击后生成 AI</p>
        </button>
      </div>

      <section className="panel watchlist-panel">
        <div className="panel-heading">
          <div><h2>关注列表 <span className="list-count">{filteredStocks.length}</span></h2><p>点击股票查看详情 · 行情每 15 分钟缓存</p></div>
        </div>
        <div className="watch-toolbar">
          <div className="research-filters"><select aria-label="自选分组" value={group} onChange={event => setGroup(event.target.value)}><option value="">全部分组</option>{groups.map(value => <option key={value}>{value}</option>)}</select><select aria-label="自选排序" value={sort} onChange={event => setSort(event.target.value)}><option value="added">按关注时间</option><option value="change">按涨跌幅</option><option value="name">按名称</option></select></div>
          <label className="search-box">
            <span aria-hidden="true">⌕</span>
            <input value={query} onChange={(event) => onQueryChange(event.target.value)} aria-label="搜索自选股" placeholder="搜索代码或名称" />
          </label>
        </div>
        {loading && !stocks.length ? (
          <div className="empty-state"><span className="loading-ring" /><strong>正在读取自选股</strong></div>
        ) : filteredStocks.length ? (
          <div className="watch-table-wrap" role="region" aria-label="自选股行情" tabIndex={0}><table className="watch-table" role="table" aria-label="自选股列表">
            <thead><tr role="row"><th scope="col">股票</th><th scope="col">行情价</th><th scope="col">涨跌幅</th><th scope="col">行业</th><th scope="col">交易日</th><th scope="col"><span className="sr-only">操作</span></th></tr></thead>
            <tbody>
            {filteredStocks.map((stock) => (
              <tr role="row" key={stock.tsCode}>
                <th scope="row" role="rowheader"><div className="stock-name">
                  <span className="market-tag">{marketLabel(stock.exchange)}</span>
                  <div><StockLink code={stock.symbol}><strong>{stock.name}</strong></StockLink><small>{stock.symbol} · {stock.groupName || '未分组'}</small><button className="watch-note-button" onClick={() => setEditing(stock)}>编辑分组与笔记{stock.note || stock.reason ? ' · 已记录' : ''}</button>{stock.referencePrice != null && <small>关注参考价 {stock.referencePrice.toFixed(2)} · 行情 {shortTradeDate(stock.referenceDate)}</small>}</div>
                </div></th>
                <td role="cell" data-label="行情价"><strong className="price">{stock.quote ? stock.quote.close.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : '—'}</strong></td>
                <td role="cell" data-label="涨跌幅"><span className={`change ${stock.quote?.pctChg == null || stock.quote.pctChg === 0 ? 'neutral' : stock.quote.pctChg > 0 ? 'up' : 'down'}`}>
                  {stock.quote?.pctChg === null || stock.quote?.pctChg === undefined
                    ? '待更新'
                    : `${stock.quote.pctChg >= 0 ? '+' : ''}${stock.quote.pctChg.toFixed(2)}%`}
                </span></td>
                <td role="cell" data-label="行业"><span className="industry">{stock.industry ?? '未分类'}</span></td>
                <td role="cell" data-label="交易日"><span className="trade-date-cell">{shortTradeDate(stock.quote?.tradeDate)}</span></td>
                <td role="cell" className="watch-row-actions"><button className="remove-button" onClick={() => onRemove(stock)} disabled={loading} aria-label={`移除${stock.name}`}>×</button></td>
              </tr>
            ))}
            </tbody>
          </table></div>
        ) : (
          <div className="empty-state"><span aria-hidden="true">⌕</span><strong>{stocks.length ? '没有找到匹配的股票' : '建立你的关注列表'}</strong><p>{stocks.length ? '试试其他名称、代码或分组' : '添加第一只股票，开始跟踪行情与研究记录'}</p><button className="refresh-button" onClick={stocks.length ? () => { onQueryChange(''); setGroup(''); } : onOpenAdd}>{stocks.length ? '清除筛选' : '添加股票'}</button></div>
        )}
      </section>
      {editing && <WatchNoteEditor key={editing.tsCode} stock={editing} close={() => setEditing(null)} save={onSaveNote} />}
      {!loading && missingAiCount > 0 && (
        <aside className="setup-banner">
          <span className="setup-icon">钥</span>
          <div>
            <strong>自选行情与规则策略免费使用</strong>
            <p>AI 选股需配置对应模型，生成状态可在「数据与任务」查看。</p>
          </div>
          <span className="setup-count">{3 - missingAiCount}/3 AI 已配置</span>
        </aside>
      )}
      <p className="data-note">实时行情优先使用东方财富备用线路，并保留 AKShare 与 BaoStock 降级；免费源不提供可用性承诺，请在交易前向券商核对。内容仅供研究，不构成投资建议。</p>
    </section>
  );
}

function WatchNoteEditor({ stock, close, save }: { stock: WatchlistStock; close: () => void; save: (note: WatchNote) => Promise<void> }) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const element = dialog.current;
    const previousFocus = document.activeElement;
    element?.showModal();
    return () => { element?.close(); if (previousFocus instanceof HTMLElement) previousFocus.focus(); };
  }, []);
  const [group, setGroup] = useState(stock.groupName || '未分组'); const [reason, setReason] = useState(stock.reason || '');
  const [note, setNote] = useState(stock.note || ''); const [busy, setBusy] = useState(false); const [error, setError] = useState<string | null>(null);
  async function submit(event: React.FormEvent) {
    event.preventDefault(); if (busy) return; setBusy(true); setError(null);
    try { await save({ tsCode: stock.tsCode, groupName: group, reason, note }); close(); }
    catch (cause) { setError(errorMessage(cause, '保存失败')); }
    finally { setBusy(false); }
  }
  return <dialog ref={dialog} className="watch-note-dialog" aria-labelledby="watch-note-title" onCancel={event => { event.preventDefault(); if (!busy) close(); }}><form className="watch-note-editor" onSubmit={submit}><h2 id="watch-note-title">{stock.name} · 观察笔记</h2><label>分组<input autoFocus maxLength={40} value={group} onChange={event => setGroup(event.target.value)} list="watch-group-options" /></label><datalist id="watch-group-options">{['重点关注', '等待回调', '长期观察', '未分组'].map(value => <option key={value} value={value} />)}</datalist><label>当时为什么关注<textarea rows={3} maxLength={1000} value={reason} onChange={event => setReason(event.target.value)} /></label><label>后续观察笔记<textarea rows={5} maxLength={4000} value={note} onChange={event => setNote(event.target.value)} /></label><p className="data-note">关注时间 {stock.addedAt || '历史记录未提供'}；旧记录不会补造关注时价格。参考价为当时可用行情，不是成交成本。</p>{error && <p role="alert" className="research-error">{error}</p>}<div className="research-filters"><button type="submit" className="add-button" disabled={busy}>{busy ? '保存中…' : '保存笔记'}</button><button type="button" className="refresh-button" onClick={close} disabled={busy}>取消</button></div></form></dialog>;
}
