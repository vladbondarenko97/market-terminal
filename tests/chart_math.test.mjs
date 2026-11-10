// node tests/chart_math.test.mjs : the chart's indicator and zone maths (options_whale/static/chart.js), plain asserts.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
const CI = createRequire(import.meta.url)('../options_whale/static/chart.js');
const close = (a, b) => assert.ok(Math.abs(a - b) < 1e-9, `${a} != ${b}`);

assert.deepEqual(CI.sma([1, 2, 3, 4, 5], 3), [null, null, 2, 3, 4]);
assert.deepEqual(CI.ema([1, 2, 3, 4, 5], 3), [null, null, 2, 3, 4]);
close(CI.wma([1, 2, 3, 4], 3)[2], 14 / 6);
const b = CI.bb([5, 5, 5, 5], 3, 2); close(b.up[3], 5); close(b.lo[3], 5);
const r = CI.rsi([1, 2, 1, 2], 2); close(r[2], 50); close(r[3], 75);
assert.equal(CI.rsi([1, 2, 3, 4, 5], 2)[4], 100);
close(CI.atr([10, 11, 14], [8, 9, 10], [9, 10, 13], 2)[2], 3);
const m = CI.macd(Array(40).fill(7), 12, 26, 9); close(m.macd[39], 0); close(m.hist[39], 0);
assert.deepEqual(CI.obv([1, 2, 2, 1], [10, 20, 30, 40]), [0, 20, 20, -20]);
close(CI.stoch([3, 4, 5], [1, 2, 3], [2, 3, 4], 3, 1).k[2], 75);
const v = CI.vwap([2, 4, 10], [2, 4, 10], [2, 4, 10], [1, 1, 5], ['a', 'a', 'b']); close(v[1], 3); close(v[2], 10);

// synthetic: 20 flat candles, then one big up candle -> one fresh demand zone at the flat range
const mk = (o, h, l, c) => ({ o, h, l, c });
const rows = Array.from({ length: 20 }, () => mk(100, 100.6, 99.6, 100.2));
rows.push(mk(100, 104.2, 99.9, 104), mk(104, 104.5, 103.5, 104.2), mk(104, 104.6, 103.8, 104.4));
const B = o => ({ o: rows.map(x => x.o), h: rows.map(x => x.h), l: rows.map(x => x.l), c: rows.map(x => x.c) });
let z = CI.zones(B(), { k: 1.5 });
assert.equal(z.length, 1); assert.equal(z[0].type, 'demand'); close(z[0].lo, 99.6); close(z[0].hi, 100.6); assert.equal(z[0].i1, null);
// a later close below the zone mitigates it
rows.push(mk(104, 104, 98, 99));
z = CI.zones(B(), { k: 1.5 });
assert.ok(z.some(x => x.type === 'demand' && x.i1 === rows.length - 1));
// k too high: nothing
assert.equal(CI.zones(B(), { k: 9 }).length, 0);
console.log('chart math ok');
