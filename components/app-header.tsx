import { useEffect, useRef, type ReactNode } from 'react';
import { shortTradeDate } from '@/lib/format';
import type { SystemStatus } from '@/lib/types';


type AppHeaderProps = {
  activeTab: 'watchlist' | 'strategies' | 'market' | 'sectors' | 'forecast' | 'digest' | 'operations';
  status: SystemStatus | null;
  tradeDate: string | null;
  onOpenWatchlist: () => void;
  onOpenStrategies: () => void;
  onOpenMarket: () => void;
  onOpenSectors: () => void;
  onOpenForecast: () => void;
  onOpenDigest?: () => void;
  onOpenOperations?: () => void;
};

export function AppHeader({
  activeTab,
  status,
  tradeDate,
  onOpenWatchlist,
  onOpenStrategies,
  onOpenMarket,
  onOpenSectors,
  onOpenForecast,
  onOpenDigest,
  onOpenOperations,
}: AppHeaderProps) {
  const navigation = useRef<HTMLElement>(null);
  useEffect(() => {
    const nav = navigation.current;
    if (!nav) return;
    const keepActiveVisible = () => {
      const active = nav.querySelector<HTMLElement>('[aria-current="page"]');
      if (!active || nav.scrollWidth <= nav.clientWidth) return;
      nav.scrollTo({ left: active.offsetLeft - nav.offsetLeft - (nav.clientWidth - active.offsetWidth) / 2 });
    };
    keepActiveVisible();
    const observer = new ResizeObserver(keepActiveVisible);
    observer.observe(nav);
    return () => observer.disconnect();
  }, [activeTab]);
  const items: { key: AppHeaderProps['activeTab']; label: string; action?: () => void; icon: ReactNode }[] = [
    { key: 'watchlist', label: '我的自选', action: onOpenWatchlist, icon: <path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L3 9.6l6.2-.9Z" /> },
    { key: 'digest', label: '每日摘要', action: onOpenDigest, icon: <><rect x="4" y="3" width="16" height="18" rx="2" /><path d="M8 8h8M8 12h8M8 16h5" /></> },
    { key: 'market', label: '大盘观察', action: onOpenMarket, icon: <><path d="M4 4v16h16M7 13l4-4 4 3 5-7" /></> },
    { key: 'sectors', label: '板块概念', action: onOpenSectors, icon: <><rect x="3" y="3" width="7" height="7" rx="1.5" /><rect x="14" y="3" width="7" height="7" rx="1.5" /><rect x="3" y="14" width="7" height="7" rx="1.5" /><rect x="14" y="14" width="7" height="7" rx="1.5" /></> },
    { key: 'strategies', label: '策略选股', action: onOpenStrategies, icon: <><path d="M5 4v16M12 4v16M19 4v16" /><path d="M2 9h6M9 15h6M16 8h6" /></> },
    { key: 'forecast', label: '概念预测', action: onOpenForecast, icon: <><path d="m3 17 6-6 4 3 8-10M15 4h6v6M3 21h18" /></> },
    { key: 'operations', label: '数据与任务', action: onOpenOperations, icon: <><rect x="3" y="4" width="18" height="16" rx="2" /><path d="m6 12 3-3 4 6 3-4h2" /></> },
  ];
  return (
    <header className="topbar">
      <button className="brand" onClick={onOpenWatchlist} aria-label="观星 A股首页">
        <span className="brand-mark">观</span><span>观星 <em>A股</em><small>A 股研究工作台</small></span>
      </button>
      <nav ref={navigation} className="main-nav" aria-label="主导航">
        {items.map(item => <button key={item.key} type="button" className={`nav-item ${activeTab === item.key ? 'active' : ''}`} onClick={item.action} aria-current={activeTab === item.key ? 'page' : undefined}>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{item.icon}</svg>
          <span>{item.label}</span>
        </button>)}
      </nav>
      <div className={`market-state ${status?.dataSource.health === 'healthy' ? 'live' : ''}`}>
        <span className="market-state-dot" />
        <div><strong>{!status ? '正在连接数据' : status.providers.marketData ? '行情数据' : '行情暂未就绪'}</strong>
          <small>{status?.providers.marketData ? `${status.dataSource.source} · ${shortTradeDate(tradeDate)}` : '可在数据与任务中查看状态'}</small>
        </div>
      </div>
    </header>
  );
}
