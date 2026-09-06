import type { WatchlistStock } from '@/lib/types';

export function summarizeWatchlist(stocks: WatchlistStock[]) {
  const tradeDate = stocks.reduce<string | null>((latest, stock) => {
    const date = stock.quote?.tradeDate;
    return date && (!latest || date > latest) ? date : latest;
  }, null);
  const changes = stocks.flatMap((stock) => {
    const quote = stock.quote;
    return quote?.tradeDate === tradeDate && quote.pctChg != null && Number.isFinite(quote.pctChg)
      ? [quote.pctChg] : [];
  });
  return {
    tradeDate,
    averageChange: changes.length ? changes.reduce((sum, value) => sum + value, 0) / changes.length : null,
    upCount: changes.filter((value) => value > 0).length,
    downCount: changes.filter((value) => value < 0).length,
    flatCount: changes.filter((value) => value === 0).length,
  };
}
