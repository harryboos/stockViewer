import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  metadataBase: new URL(process.env.NEXT_PUBLIC_SITE_URL ?? 'http://localhost:3000'),
  title: '观星 A股｜自选股与策略选股',
  description: '专注 A 股的自选股与多策略观察台。',
  openGraph: {
    title: '观星 A股｜自选股与多策略观察台',
    description: '在同一处查看你的 A 股自选、公开策略和三种 AI 每日选股观点。',
    type: 'website',
    locale: 'zh_CN',
    images: [{ url: '/og.png', width: 1200, height: 630, alt: '观星 A股，自选股与多策略观察台' }],
  },
  twitter: {
    card: 'summary_large_image',
    title: '观星 A股｜自选股与多策略观察台',
    description: '在同一处查看你的 A 股自选、公开策略和三种 AI 每日选股观点。',
    images: ['/og.png'],
  },
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
