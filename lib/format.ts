const chinaDate = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: 'long',
    day: 'numeric',
    weekday: 'long',
});

export function formatChinaDate(): string {
  return chinaDate.format(new Date());
}

export function shortTradeDate(value?: string | null): string {
  if (value && /^\d{4}-\d{2}-\d{2}$/.test(value)) return value;
  if (!value || !/^\d{8}$/.test(value)) return '等待行情';
  return `${value.slice(0, 4)}-${value.slice(4, 6)}-${value.slice(6, 8)}`;
}

export function marketLabel(exchange: string): string {
  if (exchange === 'SSE') return '沪';
  if (exchange === 'BSE') return '北';
  return '深';
}

export function percent(value: number | null | undefined) {
  return value == null || !Number.isFinite(value) ? '—' : `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`;
}

export function amount(value: number | null | undefined, signed = false) {
  if (value == null || !Number.isFinite(value)) return '—';
  return `${signed && value > 0 ? '+' : ''}${(value / 100_000_000).toFixed(2)} 亿`;
}

export function tone(value: number | null | undefined) {
  return value == null || !Number.isFinite(value) || value === 0 ? '' : value > 0 ? 'up-text' : 'down-text';
}

const chinaTimestamp = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
});

export function timestamp(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '时间暂缺' : chinaTimestamp.format(date);
}
