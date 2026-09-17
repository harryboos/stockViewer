import { createBackendHandler } from '@/lib/backend';

export const GET = createBackendHandler('/api/research/jobs');
export const POST = createBackendHandler('/api/research/jobs', { useServerDailySecret: true });
