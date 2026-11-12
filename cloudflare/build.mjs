// Copies the console UI from ../server/static into ./public (one source of truth for both servers).
import { cpSync, mkdirSync, rmSync, writeFileSync, copyFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const src = join(here, '..', 'server', 'static');
const out = join(here, 'public');
rmSync(out, { recursive: true, force: true });
mkdirSync(join(out, 'static'), { recursive: true });
cpSync(src, join(out, 'static'), { recursive: true });
copyFileSync(join(src, 'index.html'), join(out, 'index.html'));
writeFileSync(join(out, '_routes.json'), JSON.stringify({ version: 1, include: ['/api/*', '/webhook/*', '/media/*', '/report/*'], exclude: [] }, null, 2));
writeFileSync(join(out, '_headers'), `/*\n  X-Content-Type-Options: nosniff\n  Referrer-Policy: same-origin\n  X-Frame-Options: DENY\n`);
console.log('public/ ready');
