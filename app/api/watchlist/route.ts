import { createBackendHandler } from '@/lib/backend';

export const GET = createBackendHandler('/api/watchlist');
export const POST = createBackendHandler('/api/watchlist');
export const DELETE = createBackendHandler('/api/watchlist');
