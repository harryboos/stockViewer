import { createBackendHandler } from '@/lib/backend';

export const GET = createBackendHandler('/api/market/concepts/ai');
export const POST = createBackendHandler('/api/market/concepts/ai', { useServerDailySecret: true });
