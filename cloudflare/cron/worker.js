// SKYWARDEN cron: a small Worker that runs every minute against the same database as the site.
// Escalation of unacknowledged incidents, drone phases, boxes that stopped reporting, deletion of old media.
import { scheduled } from '../lib/app.js';

export default {
  async scheduled(event, env, ctx) { ctx.waitUntil(scheduled(env, ctx)); },
  async fetch() { return new Response('SKYWARDEN cron: runs every minute', { headers: { 'Content-Type': 'text/plain; charset=utf-8' } }); },
};
