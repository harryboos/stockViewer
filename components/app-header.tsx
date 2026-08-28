import { shortTradeDate } from '@/lib/format';
import type { SystemStatus } from '@/lib/types';


type AppHeaderProps = {
  activeTab: 'watchlist' | 'strategies' | 'market';
  status: SystemStatus | null;
  tradeDate: string | null;
  onOpenWatchlist: () => void;
  onOpenStrategies: () => void;
  onOpenMarket: () => void;
};

export function AppHeader({
  activeTab,
  status,
  tradeDate,
  onOpenWatchlist,
  onOpenStrategies,
  onOpenMarket,
}: AppHeaderProps) {
  return (
    <header className="topbar">
      <button className="brand" onClick={onOpenWatchlist} aria-label="观星 A股首页">
        <span className="brand-mark">观</span><span>观星 <em>A股</em></span>
      </button>
      <nav className="main-nav" aria-label="主导航">
        <button className={`nav-item ${activeTab === 'watchlist' ? 'active' : ''}`} onClick={onOpenWatchlist}>我的自选</button>
        <button className={`nav-item ${activeTab === 'strategies' ? 'active' : ''}`} onClick={onOpenStrategies}>策略选股</button>
        <button className={`nav-item ${activeTab === 'market' ? 'active' : ''}`} onClick={onOpenMarket}>大盘观察</button>
      </nav>
      <div className={`market-state ${status?.providers.marketData ? 'live' : ''}`}>
        <span />
        {status?.providers.marketData
          ? `${status.dataSource.source} · ${shortTradeDate(tradeDate)}`
          : '免费数据服务未就绪'}
      </div>
    </header>
  );
}
