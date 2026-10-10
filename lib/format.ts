const chinaDate = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: 'long',
    day: 'numeric',
    weekday: 'long',
});
const chinaDayFormat = new Intl.DateTimeFormat('sv-SE', {
  timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
});

export function chinaDay() {
  return chinaDayFormat.format(new Date());
}

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

export function isFiniteNumber(value: number | null | undefined): value is number {
  return value != null && Number.isFinite(value);
}

export function decimal(value: number | null | undefined) {
  return isFiniteNumber(value) ? value.toFixed(2) : '—';
}

// Rates (breadth, hit rate) are unsigned; returns carry an explicit sign.
export function rate(value: number | null | undefined, digits = 1) {
  return isFiniteNumber(value) ? `${value.toFixed(digits)}%` : '—';
}

export function percent(value: number | null | undefined) {
  return isFiniteNumber(value) ? `${value >= 0 ? '+' : ''}${value.toFixed(2)}%` : '—';
}

export function percentagePoints(value: number | null | undefined, missing = '—') {
  return isFiniteNumber(value) ? `${value > 0 ? '+' : ''}${value.toFixed(2)} 个百分点` : missing;
}

export function amount(value: number | null | undefined, signed = false) {
  if (!isFiniteNumber(value)) return '—';
  return `${signed && value > 0 ? '+' : ''}${(value / 100_000_000).toFixed(2)} 亿`;
}

export function compactAmount(value: number | null | undefined, signed = false) {
  if (!isFiniteNumber(value)) return '—';
  const yi = value / 100_000_000;
  const text = Math.abs(yi) >= 10_000 ? `${(yi / 10_000).toFixed(2)}万亿`
    : `${yi.toFixed(Math.abs(yi) >= 100 ? 0 : 1)}亿`;
  return `${signed && value >= 0 ? '+' : ''}${text}`;
}

export function tone(value: number | null | undefined) {
  return !isFiniteNumber(value) || value === 0 ? '' : value > 0 ? 'up-text' : 'down-text';
}

const chinaTimestamp = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
});

export function timestamp(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '时间暂缺' : chinaTimestamp.format(date);
}
