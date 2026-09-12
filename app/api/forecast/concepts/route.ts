import { createBackendHandler } from '@/lib/backend';

export const GET = createBackendHandler('/api/forecast/concepts');
export const POST = createBackendHandler('/api/forecast/concepts', { useServerDailySecret: true });
