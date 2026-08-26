'use client';

import { useEffect, useMemo, useState } from 'react';

type Stock = {
  code: string;
  name: string;
  market: '沪' | '深' | '科';
  price: number;
  change: number;
  industry: string;
  weight: number;
};

const STOCK_CATALOG: Stock[] = [
  { code: '600519', name: '贵州茅台', market: '沪', price: 1428.8, change: 1.26, industry: '食品饮料', weight: 28.4 },
  { code: '300750', name: '宁德时代', market: '深', price: 282.45, change: 2.14, industry: '电力设备', weight: 21.7 },
  { code: '601318', name: '中国平安', market: '沪', price: 58.72, change: -0.41, industry: '非银金融', weight: 18.5 },
  { code: '000858', name: '五粮液', market: '深', price: 132.16, change: 0.83, industry: '食品饮料', weight: 12.9 },
  { code: '600036', name: '招商银行', market: '沪', price: 46.21, change: 0.34, industry: '银行', weight: 10.2 },
  { code: '688981', name: '中芯国际', market: '科', price: 91.36, change: -1.18, industry: '半导体', weight: 8.3 },
  { code: '600900', name: '长江电力', market: '沪', price: 27.48, change: 0.22, industry: '公用事业', weight: 0 },
  { code: '601088', name: '中国神华', market: '沪', price: 42.65, change: 0.76, industry: '煤炭', weight: 0 },
  { code: '000333', name: '美的集团', market: '深', price: 76.18, change: 1.05, industry: '家用电器', weight: 0 },
  { code: '600276', name: '恒瑞医药', market: '沪', price: 62.39, change: -0.68, industry: '医药生物', weight: 0 },
  { code: '601899', name: '紫金矿业', market: '沪', price: 29.61, change: 2.48, industry: '有色金属', weight: 0 },
  { code: '300308', name: '中际旭创', market: '深', price: 234.8, change: 3.12, industry: '通信', weight: 0 },
];

const DEFAULT_CODES = ['600519', '300750', '601318', '000858', '600036', '688981'];

const PUBLIC_STRATEGIES = [
  {
    id: 'dividend',
    index: '公开策略 01',
    name: '红利质量',
    desc: '偏好连续分红、股息率较高，同时盈利持续性更好的公司。',
    tags: ['连续现金分红', '高股息率', '盈利质量'],
    color: 'forest',
    source: '上海证券交易所 · 上证红利质量指数',
    url: 'https://www.sse.com.cn/market/sseindex/diclosure/c/c_20250123_10770595.shtml',
    picks: [
      ['601088', '中国神华', '煤炭'], ['600900', '长江电力', '公用事业'], ['000333', '美的集团', '家用电器'], ['600036', '招商银行', '银行'],
    ],
  },
  {
    id: 'momentum',
    index: '公开策略 02',
    name: '动量成长',
    desc: '结合营收与利润增长、52 周新高和 12-1 月价格动量筛选。',
    tags: ['收入增长', '利润增长', '价格动量'],
    color: 'clay',
    source: '国证指数 · 创业板动量成长指数方法',
    url: 'https://www.cnindex.com.cn/docs/gz_399296.pdf',
    picks: [
      ['300750', '宁德时代', '电力设备'], ['300308', '中际旭创', '通信'], ['601899', '紫金矿业', '有色金属'], ['600276', '恒瑞医药', '医药生物'],
    ],
  },
  {
    id: 'lowvol',
    index: '公开策略 03',
    name: '小盘低波',
    desc: '在小盘样本中按近一年日收益波动率升序选择，追求稳健暴露。',
    tags: ['小盘股', '一年波动率', '流动性筛选'],
    color: 'indigo',
    source: '国证指数 · 巨潮小盘低波指数方法',
    url: 'https://www.cnindex.com.cn/docs/gz_399408.pdf',
    picks: [
      ['002372', '伟星新材', '建筑材料'], ['600153', '建发股份', '交通运输'], ['002867', '周大生', '纺织服饰'], ['600901', '江苏金租', '非银金融'],
    ],
  },
];

const AI_STRATEGIES = [
  {
    id: 'deepseek',
    model: 'DeepSeek',
    monogram: 'D',
    theme: 'deepseek',
    title: '深度价值',
    desc: '从估值安全边际、现金流与盈利修复三个维度交叉验证。',
    logic: '低估值 · 强现金流 · 盈利修复',
    universe: [
      ['601088', '中国神华', 92], ['600036', '招商银行', 89], ['601318', '中国平安', 86], ['000333', '美的集团', 84], ['600900', '长江电力', 81],
    ],
  },
  {
    id: 'gemini',
    model: 'Gemini',
    monogram: 'G',
    theme: 'gemini',
    title: '成长雷达',
    desc: '关注产业景气、营收增速和价格趋势之间的共振。',
    logic: '行业景气 · 高成长 · 趋势确认',
    universe: [
      ['300308', '中际旭创', 94], ['300750', '宁德时代', 90], ['601899', '紫金矿业', 87], ['688981', '中芯国际', 85], ['600276', '恒瑞医药', 82],
    ],
  },
  {
    id: 'chatgpt',
    model: 'ChatGPT',
    monogram: 'C',
    theme: 'chatgpt',
    title: '质量均衡',
    desc: '综合竞争壁垒、资产负债表质量与组合行业分散度。',
    logic: '商业质量 · 财务稳健 · 行业均衡',
    universe: [
      ['600519', '贵州茅台', 93], ['000333', '美的集团', 91], ['600900', '长江电力', 88], ['600036', '招商银行', 85], ['600276', '恒瑞医药', 83],
    ],
  },
];

function getDayKey() {
  return new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' })
    .format(new Date())
    .replaceAll('/', '-');
}

function formatDate() {
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: 'long', day: 'numeric', weekday: 'long',
  }).format(new Date());
}

export default function Home() {
  const [activeTab, setActiveTab] = useState<'watchlist' | 'strategies'>('watchlist');
  const [watchCodes, setWatchCodes] = useState(DEFAULT_CODES);
  const [query, setQuery] = useState('');
  const [addQuery, setAddQuery] = useState('');
  const [modalOpen, setModalOpen] = useState(false);
  const [toast, setToast] = useState('');
  const [today, setToday] = useState('今日');

  useEffect(() => {
    setToday(formatDate());
    const saved = window.localStorage.getItem('guanxing-watchlist');
    if (saved) {
      try { setWatchCodes(JSON.parse(saved)); } catch { /* keep defaults */ }
    }
    const day = getDayKey();
    const lastRun = window.localStorage.getItem('guanxing-ai-last-run');
    if (lastRun !== day) window.localStorage.setItem('guanxing-ai-last-run', day);
  }, []);

  useEffect(() => {
    window.localStorage.setItem('guanxing-watchlist', JSON.stringify(watchCodes));
  }, [watchCodes]);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(''), 2200);
    return () => window.clearTimeout(timer);
  }, [toast]);

  const stocks = useMemo(() => watchCodes.map((code) => STOCK_CATALOG.find((stock) => stock.code === code)).filter(Boolean) as Stock[], [watchCodes]);
  const filteredStocks = stocks.filter((stock) => `${stock.name}${stock.code}${stock.industry}`.toLowerCase().includes(query.toLowerCase()));
  const addableStocks = STOCK_CATALOG.filter((stock) => !watchCodes.includes(stock.code) && `${stock.name}${stock.code}${stock.industry}`.toLowerCase().includes(addQuery.toLowerCase()));
  const avgChange = stocks.length ? stocks.reduce((sum, stock) => sum + stock.change, 0) / stocks.length : 0;
  const upCount = stocks.filter((stock) => stock.change >= 0).length;

  function addStock(code: string, name: string) {
    setWatchCodes((codes) => [...codes, code]);
    setToast(`已将 ${name} 加入自选`);
  }

  function removeStock(code: string, name: string) {
    setWatchCodes((codes) => codes.filter((item) => item !== code));
    setToast(`已将 ${name} 移出自选`);
  }

  return (
    <main className="app-shell">
      <header className="topbar">
        <button className="brand" onClick={() => setActiveTab('watchlist')} aria-label="观星 A股首页">
          <span className="brand-mark">观</span>
          <span>观星 <em>A股</em></span>
        </button>
        <nav className="main-nav" aria-label="主导航">
          <button className={`nav-item ${activeTab === 'watchlist' ? 'active' : ''}`} onClick={() => setActiveTab('watchlist')}>我的自选</button>
          <button className={`nav-item ${activeTab === 'strategies' ? 'active' : ''}`} onClick={() => setActiveTab('strategies')}>策略选股</button>
        </nav>
        <div className="market-state"><span /> A股已收盘 · 演示行情</div>
      </header>

      {activeTab === 'watchlist' ? (
        <section className="content page-enter">
          <div className="hero-row">
            <div>
              <p className="eyebrow">我的投资清单</p>
              <h1>看看今天的自选股</h1>
              <p className="subtitle">{today} · 共关注 {stocks.length} 只股票</p>
            </div>
            <button className="add-button" onClick={() => setModalOpen(true)}><span>＋</span> 添加股票</button>
          </div>

          <div className="summary-grid">
            <article className="summary-card dark-card">
              <div className="card-topline"><span>自选组合今日</span><span className="status-pill">演示</span></div>
              <strong className="metric-large">{avgChange >= 0 ? '+' : ''}{avgChange.toFixed(2)}%</strong>
              <div className="micro-chart" aria-hidden="true">
                {[30, 42, 36, 58, 51, 72, 85, 78].map((height) => <i key={height} style={{ height: `${height}%` }} />)}
              </div>
            </article>
            <article className="summary-card">
              <span className="card-label">上涨 / 下跌</span>
              <strong className="metric"><b>{upCount}</b><small> / {stocks.length - upCount}</small></strong>
              <p className="card-note positive">组合红绿分布</p>
            </article>
            <article className="summary-card">
              <span className="card-label">关注行业</span>
              <strong className="metric">{new Set(stocks.map((stock) => stock.industry)).size}<small> 个</small></strong>
              <p className="card-note">建议留意行业集中度</p>
            </article>
            <button className="summary-card accent-card signal-card" onClick={() => setActiveTab('strategies')}>
              <span className="card-label">策略新信号 <i>→</i></span>
              <strong className="metric"><b>7</b><small> 条</small></strong>
              <p className="card-note">3 个 AI 策略今日已运行</p>
            </button>
          </div>

          <section className="panel">
            <div className="panel-heading">
              <div><h2>自选股</h2><p>你最关注的 A 股，一眼掌握</p></div>
              <label className="search-box"><span>⌕</span><input value={query} onChange={(event) => setQuery(event.target.value)} aria-label="搜索股票" placeholder="搜索代码或名称" /></label>
            </div>
            {filteredStocks.length ? (
              <div className="stock-table" role="table" aria-label="自选股列表">
                <div className="stock-row table-head" role="row"><span>股票</span><span>最新价</span><span>涨跌幅</span><span>行业</span><span>组合占比</span><span /></div>
                {filteredStocks.map((stock) => (
                  <div className="stock-row" role="row" key={stock.code}>
                    <div className="stock-name"><span className="market-tag">{stock.market}</span><div><strong>{stock.name}</strong><small>{stock.code}</small></div></div>
                    <strong className="price">{stock.price.toLocaleString('zh-CN', { minimumFractionDigits: 2 })}</strong>
                    <span className={`change ${stock.change >= 0 ? 'up' : 'down'}`}>{stock.change >= 0 ? '+' : ''}{stock.change.toFixed(2)}%</span>
                    <span className="industry">{stock.industry}</span>
                    <div className="weight"><span><i style={{ width: `${Math.min(stock.weight * 2.8, 100)}%` }} /></span><small>{stock.weight || '—'}{stock.weight ? '%' : ''}</small></div>
                    <button className="remove-button" onClick={() => removeStock(stock.code, stock.name)} aria-label={`移除${stock.name}`}>×</button>
                  </div>
                ))}
              </div>
            ) : (
              <div className="empty-state"><span>⌕</span><strong>没有找到匹配的股票</strong><p>换一个名称或股票代码试试</p></div>
            )}
          </section>
          <p className="data-note">行情为界面演示数据，并非实时价格。网站内容仅供策略研究，不构成任何投资建议。</p>
        </section>
      ) : (
        <section className="content strategies-page page-enter">
          <div className="strategy-hero">
            <div>
              <p className="eyebrow">多视角选股实验室</p>
              <h1>把不同方法，放在同一张桌上</h1>
              <p className="subtitle">公开指数方法与 AI 模型每日观点并列呈现，先看逻辑，再看结果。</p>
            </div>
            <div className="daily-status"><span className="pulse-dot" /><div><strong>今日策略已更新</strong><small>每日运行一次 · {today}</small></div></div>
          </div>

          <section className="strategy-section">
            <div className="section-heading"><div><span className="section-index">01</span><div><h2>公开策略</h2><p>依据交易所及指数公司的公开编制方案整理</p></div></div><span className="source-count">3 种方法</span></div>
            <div className="public-grid">
              {PUBLIC_STRATEGIES.map((strategy) => (
                <article className={`public-card ${strategy.color}`} key={strategy.id}>
                  <div className="strategy-card-top"><span>{strategy.index}</span><span className="method-mark">◌</span></div>
                  <h3>{strategy.name}</h3>
                  <p>{strategy.desc}</p>
                  <div className="tag-row">{strategy.tags.map((tag) => <span key={tag}>{tag}</span>)}</div>
                  <div className="mini-picks">
                    {strategy.picks.map(([code, name, industry], index) => (
                      <div key={code}><span className="pick-rank">0{index + 1}</span><strong>{name}</strong><small>{code} · {industry}</small></div>
                    ))}
                  </div>
                  <a href={strategy.url} target="_blank" rel="noreferrer" className="source-link">查看编制来源 <span>↗</span><small>{strategy.source}</small></a>
                </article>
              ))}
            </div>
          </section>

          <section className="strategy-section ai-section">
            <div className="section-heading"><div><span className="section-index">02</span><div><h2>AI 每日选股</h2><p>三种模型、三套提示框架；每天固定生成一次研究样例</p></div></div><span className="source-count ai-ready"><i /> 今日已运行</span></div>
            <div className="ai-grid">
              {AI_STRATEGIES.map((strategy) => (
                <article className={`ai-card ${strategy.theme}`} key={strategy.id}>
                  <div className="ai-card-heading"><span className="ai-monogram">{strategy.monogram}</span><div><small>{strategy.model}</small><h3>{strategy.title}</h3></div><span className="run-badge">每日</span></div>
                  <p className="ai-desc">{strategy.desc}</p>
                  <div className="logic-line"><span>选股逻辑</span><strong>{strategy.logic}</strong></div>
                  <div className="ai-picks">
                    {strategy.universe.slice(0, 3).map(([code, name, score], index) => (
                      <div key={String(code)}>
                        <span className="rank-circle">{index + 1}</span>
                        <div><strong>{name}</strong><small>{code}</small></div>
                        <div className="score"><i style={{ width: `${Number(score) - 28}%` }} /><span>{score}</span></div>
                      </div>
                    ))}
                  </div>
                  <div className="ai-footer"><span>模型研究样例</span><span>08:30 生成</span></div>
                </article>
              ))}
            </div>
          </section>

          <aside className="risk-banner"><span>研</span><div><strong>先理解方法，再使用结果</strong><p>策略结果与 AI 输出均为研究演示，不代表收益承诺；公开策略只借鉴筛选思想，示例股票不等同于指数最新成份股。</p></div></aside>
        </section>
      )}

      {modalOpen && (
        <div className="modal-backdrop" role="presentation" onMouseDown={() => setModalOpen(false)}>
          <section className="modal" role="dialog" aria-modal="true" aria-labelledby="add-title" onMouseDown={(event) => event.stopPropagation()}>
            <div className="modal-head"><div><p className="eyebrow">扩展你的观察池</p><h2 id="add-title">添加 A 股</h2></div><button onClick={() => setModalOpen(false)} aria-label="关闭">×</button></div>
            <label className="modal-search"><span>⌕</span><input autoFocus value={addQuery} onChange={(event) => setAddQuery(event.target.value)} placeholder="输入股票代码、名称或行业" /></label>
            <div className="catalog-list">
              {addableStocks.length ? addableStocks.map((stock) => (
                <div key={stock.code}><span className="market-tag">{stock.market}</span><div><strong>{stock.name}</strong><small>{stock.code} · {stock.industry}</small></div><span className={`catalog-change ${stock.change >= 0 ? 'up-text' : 'down-text'}`}>{stock.change >= 0 ? '+' : ''}{stock.change.toFixed(2)}%</span><button onClick={() => addStock(stock.code, stock.name)}>添加</button></div>
              )) : <div className="modal-empty">没有更多匹配股票</div>}
            </div>
            <p className="modal-footnote">当前为演示股票池；自选记录会保存在此设备。</p>
          </section>
        </div>
      )}

      {toast && <div className="toast" role="status"><span>✓</span>{toast}</div>}
    </main>
  );
}
