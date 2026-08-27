export function formatChinaDate(): string {
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: 'long',
    day: 'numeric',
    weekday: 'long',
  }).format(new Date());
}

export function shortTradeDate(value?: string | null): string {
  if (!value || value.length !== 8) return '等待行情';
  return `${value.slice(0, 4)}-${value.slice(4, 6)}-${value.slice(6, 8)}`;
}

export function marketLabel(exchange: string): string {
  if (exchange === 'SSE') return '沪';
  if (exchange === 'BSE') return '北';
  return '深';
}
