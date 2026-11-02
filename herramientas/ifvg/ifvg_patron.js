// Patron AMD + IFVG (acumulacion, manipulacion por barrido, distribucion con FVG invertido).
// Funcion pura sobre velas {t,o,h,l,c}. Devuelve la senal en la ultima vela cerrada o null.
//
// Secuencia (version bajista; la alcista es espejo):
//   1. Rango: en las ultimas N velas el precio oscila entre un alto y un bajo (acumulacion).
//   2. Manipulacion: una vela rompe el alto del rango y se forma un FVG ALCISTA en ese impulso
//      (el hueco entre el alto de la vela i-1 y el bajo de la vela i+1).
//   3. Inversion: una vela posterior CIERRA por debajo del FVG (y de vuelta dentro del rango):
//      ese FVG deja de ser soporte y pasa a ser resistencia (IFVG).
//   4. Entrada: la primera vela que vuelve a tocar el IFVG (retest) sin cerrar por encima.
//      Entrada = cierre de esa vela; stop = maximo del barrido + margen; objetivo = R multiplo.
function detectarIFVG(velas, opt) {
  opt = opt || {};
  const N = opt.rango || 24, maxEsperaInv = opt.esperaInversion || 6, maxEsperaRet = opt.esperaRetest || 8;
  const rr = opt.rr || 2, margen = opt.margenAtr || 0.1, minFvgAtr = opt.minFvgAtr || 0.15;
  const n = velas.length;
  if (n < N + 20) return null;
  const ult = n - 1;
  let atr = 0; for (let i = n - 15; i < n; i++) atr += velas[i].h - velas[i].l; atr /= 15;
  if (!atr) return null;
  // Buscamos la vela de retest en la ultima posicion: recorremos hacia atras posibles barridos
  for (let iB = ult - 2; iB >= Math.max(N + 2, ult - (maxEsperaInv + maxEsperaRet + 2)); iB--) {
    for (const lado of ['BAJISTA', 'ALCISTA']) {
      const r = _rango(velas, iB - 1, N);
      if (!r) continue;
      const v = velas[iB], prev = velas[iB - 1], next = velas[iB + 1];
      if (lado === 'BAJISTA') {
        if (!(v.h > r.alto && prev.h <= r.alto)) continue;                     // barrido del alto del rango
        if (!(next && next.l > prev.h)) continue;                              // FVG alcista: hueco prev.h .. next.l
        const fTop = next.l, fBot = prev.h;
        if (fTop - fBot < atr * minFvgAtr) continue;
        const barridoMax = Math.max(v.h, next.h);
        // inversion: primera vela despues de next que cierra por debajo del FVG
        let iInv = -1;
        for (let k = iB + 2; k <= Math.min(ult, iB + 1 + maxEsperaInv); k++) {
          if (velas[k].h > barridoMax + atr * 0.05) { iInv = -2; break; }     // siguio subiendo: no hay manipulacion
          if (velas[k].c < fBot) { iInv = k; break; }
        }
        if (iInv < 0) continue;
        // retest: primera vela despues de la inversion que toca el IFVG sin cerrar por encima de el
        for (let k = iInv + 1; k <= Math.min(ult, iInv + maxEsperaRet); k++) {
          const w = velas[k];
          if (w.c > fTop) break;                                                // reconquisto el FVG: patron fallido
          if (w.h >= fBot) {
            if (k !== ult) break;                                               // el retest ya paso; no es ahora
            const sl = barridoMax + atr * margen, entrada = w.c, riesgo = sl - entrada;
            if (riesgo <= 0 || riesgo > atr * 3) return null;
            return { tipo: 'IFVG', direccion: 'VENTA', entrada, sl, tp: entrada - riesgo * rr, riesgo, atr,
                     rango: r, fvg: { desde: fBot, hasta: fTop }, i_barrido: iB, i_inversion: iInv, i_retest: k };
          }
        }
      } else {
        if (!(v.l < r.bajo && prev.l >= r.bajo)) continue;
        if (!(next && next.h < prev.l)) continue;                              // FVG bajista: hueco next.h .. prev.l
        const fTop = prev.l, fBot = next.h;
        if (fTop - fBot < atr * minFvgAtr) continue;
        const barridoMin = Math.min(v.l, next.l);
        let iInv = -1;
        for (let k = iB + 2; k <= Math.min(ult, iB + 1 + maxEsperaInv); k++) {
          if (velas[k].l < barridoMin - atr * 0.05) { iInv = -2; break; }
          if (velas[k].c > fTop) { iInv = k; break; }
        }
        if (iInv < 0) continue;
        for (let k = iInv + 1; k <= Math.min(ult, iInv + maxEsperaRet); k++) {
          const w = velas[k];
          if (w.c < fBot) break;
          if (w.l <= fTop) {
            if (k !== ult) break;
            const sl = barridoMin - atr * margen, entrada = w.c, riesgo = entrada - sl;
            if (riesgo <= 0 || riesgo > atr * 3) return null;
            return { tipo: 'IFVG', direccion: 'COMPRA', entrada, sl, tp: entrada + riesgo * rr, riesgo, atr,
                     rango: r, fvg: { desde: fBot, hasta: fTop }, i_barrido: iB, i_inversion: iInv, i_retest: k };
          }
        }
      }
    }
  }
  return null;
}
// Rango de acumulacion: las N velas que terminan en `hasta` deben tener una amplitud contenida
// (alto-bajo <= 6 ATR) y al menos 2 toques cerca de cada extremo.
function _rango(velas, hasta, N) {
  const desde = hasta - N + 1;
  if (desde < 0) return null;
  let alto = -Infinity, bajo = Infinity, atr = 0;
  for (let i = desde; i <= hasta; i++) { alto = Math.max(alto, velas[i].h); bajo = Math.min(bajo, velas[i].l); atr += velas[i].h - velas[i].l; }
  atr /= N;
  const amp = alto - bajo;
  if (!atr || amp > atr * 6) return null;
  const tol = amp * 0.15;
  let tA = 0, tB = 0;
  for (let i = desde; i <= hasta; i++) { if (velas[i].h >= alto - tol) tA++; if (velas[i].l <= bajo + tol) tB++; }
  if (tA < 2 || tB < 2) return null;
  return { alto, bajo, desde, hasta, atr };
}
export { detectarIFVG };
