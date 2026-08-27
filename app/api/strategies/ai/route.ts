import { createBackendHandler } from '@/lib/backend';

export const GET = createBackendHandler('/api/strategies/ai');
export const POST = createBackendHandler('/api/strategies/ai', { useServerDailySecret: true });
