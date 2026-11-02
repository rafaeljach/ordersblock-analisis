import { detectarIFVG } from './ifvg_patron.js';
const chk = (n, c, x) => console.log((c ? 'CORRECTO ' : 'FALLA    ') + n + (x ? '  ' + x : ''));
const V = (o,h,l,c,i) => ({ t: i * 900000, o, h, l, c });
// Rango 100-110 durante 30 velas, barrido a 114 con FVG alcista, inversion, retest
const velas = [];
for (let i = 0; i < 50; i++) { const base = 100 + (i % 2 ? 9 : 1); velas.push(V(base, Math.min(110, base + 1.5), Math.max(100, base - 1.5), base + (i % 2 ? -0.5 : 0.5), i)); }
velas.push(V(108, 109, 107, 108.5, 50));        // prev (i-1): h=109
velas.push(V(108.5, 114, 108.4, 113.5, 51));    // barrido: h=114 > 110
velas.push(V(113.5, 114.2, 111, 112, 52));      // next: l=111 > prev.h=109 -> FVG 109..111
velas.push(V(112, 112.5, 108, 108.2, 53));      // inversion: cierra bajo 109
velas.push(V(108.2, 110.5, 107.5, 108.8, 54));  // retest: toca 109..111 sin cerrar sobre 111
const s = detectarIFVG(velas);
chk('1. Detecta el IFVG bajista en el retest: venta, stop sobre el barrido, TP a 2R', s && s.direccion === 'VENTA' && s.sl > 114 && s.entrada === 108.8 && Math.abs((s.entrada - s.tp) / s.riesgo - 2) < 1e-9, JSON.stringify(s && { d: s.direccion, e: s.entrada, sl: s.sl, tp: s.tp }));
chk('2. Una vela antes del retest no hay senal', detectarIFVG(velas.slice(0, -1)) === null);
const roto = velas.slice(0, -1); roto.push(V(108.2, 112, 108, 111.8, 54));
chk('3. Si el retest cierra por encima del FVG (lo reconquista) no hay senal', detectarIFVG(roto) === null);
// Espejo alcista
const esp = velas.map(v => ({ t: v.t, o: 220 - v.o, h: 220 - v.l, l: 220 - v.h, c: 220 - v.c }));
const s2 = detectarIFVG(esp);
chk('4. Espejo: IFVG alcista = compra con stop bajo el barrido', s2 && s2.direccion === 'COMPRA' && s2.sl < 106 && s2.tp > s2.entrada, JSON.stringify(s2 && { d: s2.direccion, sl: s2.sl }));
// Ruido aleatorio: no debe disparar casi nunca
let n = 0; let p = 100; const r = []; let seed = 7; const rnd = () => (seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648;
for (let i = 0; i < 3000; i++) { const o = p, c = p * (1 + (rnd() - 0.5) * 0.01); r.push(V(o, Math.max(o, c) * (1 + rnd() * 0.003), Math.min(o, c) * (1 - rnd() * 0.003), c, i)); p = c; }
for (let i = 60; i < r.length; i++) if (detectarIFVG(r.slice(0, i + 1))) n++;
chk('5. En 3000 velas de ruido aleatorio dispara pocas veces (<1%)', n < 30, n + ' senales');
process.exit(0);
