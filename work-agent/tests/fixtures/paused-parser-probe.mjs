// Local raw HTTP fixture, following the failure condition documented in
// https://github.com/nodejs/undici/issues/5360. No global exception handlers.
import net from 'node:net';
import { setTimeout as pause } from 'node:timers/promises';
import { backendFetch } from '../../dist/backend-http.js';
const mode = process.argv[2], size = Number(process.argv[3]);
if (!['baseline', 'candidate'].includes(mode) || ![65536, 98304, 131072].includes(size)) throw Error('Invalid fixture');
const peers = new Set();
const server = net.createServer(peer => {
    peers.add(peer); peer.on('error', () => {}); peer.on('close', () => peers.delete(peer));
    peer.once('data', () => {
        const bytes = Buffer.alloc(size, 65);
        peer.end(Buffer.concat([Buffer.from(`HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: ${bytes.length}\r\n\r\n`), bytes]));
    });
});
await new Promise(r => server.listen(0, '127.0.0.1', r));
const url = `http://127.0.0.1:${server.address().port}/`;
const response = await (mode === 'baseline' ? fetch(url) : backendFetch(url, { redirect: 'error', signal: AbortSignal.timeout(1500) }));
await pause(160); // Consumer deliberately leaves the body paused across FIN.
const data = Buffer.from(await response.arrayBuffer());
if (data.length !== size) throw Error('Incomplete fixture body');
for (const peer of peers) peer.destroy();
await new Promise(r => server.close(r));
console.log(JSON.stringify({ completed: true, bytes: data.length, mode }));
