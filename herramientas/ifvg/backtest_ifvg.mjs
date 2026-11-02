// Backtest del patron AMD + IFVG sobre velas reales de 15 min de Bybit (publico, sin clave).
// Uso en el droplet:  node backtest_ifvg.mjs [dias] [simbolos separados por coma]
//   node backtest_ifvg.mjs 180 BTCUSDT,ETHUSDT,SOLUSDT,XAUUSDT
// Reglas de cierre: stop o TP tocados por las velas siguientes (si ambos en la misma vela, cuenta como perdida).
// Expira a las 16 velas (4 h) al cierre. Sin comisiones: el resultado real es algo peor.
import { detectarIFVG } from './ifvg_patron.js';

const dias = parseInt(process.argv[2] || '180', 10);
const simbolos = (process.argv[3] || 'BTCUSDT,ETHUSDT,SOLUSDT,XAUUSDT').split(',');
const VIDA = 16, RR = 2;

async function velasBybit(sym, desdeMs, hastaMs) {
  const out = [];
  let fin = hastaMs;
  while (fin > desdeMs) {
    const u = `https://api.bybit.com/v5/market/kline?category=linear&symbol=${sym}&interval=15&limit=1000&end=${fin}`;
    const r = await fetch(u); const d = await r.json();
    const l = (d.result && d.result.list) || [];
    if (!l.length) break;
    for (const k of l) out.push({ t: +k[0], o: +k[1], h: +k[2], l: +k[3], c: +k[4] });
    const masViejo = Math.min(...l.map(k => +k[0]));
    if (masViejo >= fin) break;
    fin = masViejo - 1;
    await new Promise(r => setTimeout(r, 120));
  }
  const m = new Map(); for (const v of out) if (v.t >= desdeMs) m.set(v.t, v);
  return [...m.values()].sort((a, b) => a.t - b.t);
}

function cerrar(sn, velas, iDesde) {
  const largo = sn.direccion === 'COMPRA';
  for (let k = iDesde; k < Math.min(velas.length, iDesde + VIDA); k++) {
    const v = velas[k];
    const sl = largo ? v.l <= sn.sl : v.h >= sn.sl, tp = largo ? v.h >= sn.tp : v.l <= sn.tp;
    if (sl) return { res: 'LOSS', r: -1, k };
    if (tp) return { res: 'WIN', r: RR, k };
  }
  const k = Math.min(velas.length - 1, iDesde + VIDA - 1), v = velas[k];
  const r = (largo ? v.c - sn.entrada : sn.entrada - v.c) / sn.riesgo;
  return { res: r > 0 ? 'EXP+' : 'EXP-', r: Math.round(r * 100) / 100, k };
}

const total = { n: 0, win: 0, loss: 0, r: 0 };
console.log(`Backtest AMD+IFVG · ${dias} dias · 15 min · RR ${RR} · vida ${VIDA} velas`);
for (const sym of simbolos) {
  const hasta = Date.now(), desde = hasta - dias * 86400000;
  let velas;
  try { velas = await velasBybit(sym, desde, hasta); } catch (e) { console.log(`${sym}: sin velas (${e.message})`); continue; }
  const st = { n: 0, win: 0, loss: 0, expP: 0, expN: 0, r: 0, largos: 0, cortos: 0 };
  let bloqueoHasta = -1;
  for (let i = 60; i < velas.length - 1; i++) {
    if (i <= bloqueoHasta) continue;
    const sn = detectarIFVG(velas.slice(0, i + 1), { rr: RR });
    if (!sn) continue;
    const c = cerrar(sn, velas, i + 1);
    st.n++; st.r += c.r; if (c.res === 'WIN') st.win++; else if (c.res === 'LOSS') st.loss++; else if (c.res === 'EXP+') st.expP++; else st.expN++;
    if (sn.direccion === 'COMPRA') st.largos++; else st.cortos++;
    bloqueoHasta = c.k;
  }
  const decididas = st.win + st.loss, acierto = decididas ? Math.round(1000 * st.win / decididas) / 10 : 0;
  console.log(`${sym.padEnd(9)} velas ${velas.length}  senales ${st.n} (C ${st.largos}/V ${st.cortos})  WIN ${st.win}  LOSS ${st.loss}  EXP+ ${st.expP}  EXP- ${st.expN}  acierto TP/SL ${acierto}%  R total ${st.r.toFixed(1)}  R/senal ${st.n ? (st.r / st.n).toFixed(2) : '-'}`);
  total.n += st.n; total.win += st.win; total.loss += st.loss; total.r += st.r;
}
const dec = total.win + total.loss;
console.log(`\nTOTAL senales ${total.n}  acierto TP/SL ${dec ? Math.round(1000 * total.win / dec) / 10 : 0}%  R total ${total.r.toFixed(1)}  R/senal ${total.n ? (total.r / total.n).toFixed(2) : '-'}`);
console.log('Umbral del proyecto: solo se publica si el acierto verificado supera 80% con muestra suficiente (>= 30 senales).');
