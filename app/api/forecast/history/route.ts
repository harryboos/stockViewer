import { createBackendHandler } from '@/lib/backend';

export const GET = createBackendHandler('/api/forecast/history');
export const POST = createBackendHandler('/api/forecast/history', { useServerDailySecret: true });
