// Pantalla de ejemplo para Orders Wallet (Expo / React Native).
// Muestra el análisis SIN IA y las predicciones de 5/15/30/60 min.
// No depende de ninguna librería extra: solo React Native y fetch.
//
// Ajusta API_URL y API_KEY (mejor desde tu config/backend, ver README).

import React, { useEffect, useState } from 'react';
import { ActivityIndicator, Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';

const API_URL = 'https://analisis.ordersblock.com';
const API_KEY = 'PON_AQUI_LA_CLAVE'; // ver README: preferible pasar por tu backend

const CRIPTOS = ['BTC', 'ETH', 'SOL', 'XRP', 'ADA', 'DOGE', 'LINK', 'AVAX'];
const VERDE = '#22c55e', ROJO = '#ef4444', GRIS = '#9ca3af', FONDO = '#0b0f0c', TARJETA = '#141a16';

async function pedir(ruta) {
  const r = await fetch(`${API_URL}${ruta}`, { headers: { 'X-API-Key': API_KEY } });
  const j = await r.json();
  if (!j.ok) throw new Error(j.error || 'Error');
  return j.datos;
}

export default function PantallaAnalisis() {
  const [mercado, setMercado] = useState('BTC');
  const [bloques, setBloques] = useState([]);
  const [resultado, setResultado] = useState(null);
  const [cargando, setCargando] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    pedir('/bloques').then(setBloques).catch(() => {});
    const t = setInterval(() => pedir('/bloques').then(setBloques).catch(() => {}), 30000);
    return () => clearInterval(t);
  }, []);

  const ejecutar = async (ruta) => {
    setCargando(true); setError(null); setResultado(null);
    try { setResultado(await pedir(ruta)); } catch (e) { setError(e.message); }
    setCargando(false);
  };

  return (
    <ScrollView style={s.fondo} contentContainerStyle={{ padding: 16 }}>
      <Text style={s.titulo}>Análisis CBS</Text>
      <View style={s.fila}>
        {CRIPTOS.map((m) => (
          <Pressable key={m} onPress={() => setMercado(m)}
            style={[s.chip, mercado === m && { borderColor: VERDE }]}>
            <Text style={[s.chipTxt, mercado === m && { color: VERDE }]}>{m}</Text>
          </Pressable>
        ))}
      </View>

      <Text style={s.sub}>Operación (24 h)</Text>
      <View style={s.fila}>
        <Boton texto="A mercado" onPress={() => ejecutar(`/analisis/${mercado}?tipo=mercado`)} />
        <Boton texto="Precisión" onPress={() => ejecutar(`/analisis/${mercado}?tipo=precision`)} />
      </View>

      <Text style={s.sub}>¿Arriba o abajo al cierre del bloque?</Text>
      <View style={s.fila}>
        {bloques.map((b) => (
          <Boton key={b.minutos} texto={`${b.minutos}m → ${b.vence_utc}`}
            onPress={() => ejecutar(`/prediccion/${mercado}?minutos=${b.minutos}`)} />
        ))}
      </View>

      {cargando && <ActivityIndicator color={VERDE} style={{ marginTop: 24 }} />}
      {error && <Text style={[s.txt, { color: ROJO, marginTop: 16 }]}>{error}</Text>}
      {resultado && (resultado.probabilidad_pct !== undefined
        ? <TarjetaPrediccion p={resultado} />
        : <TarjetaAnalisis a={resultado} />)}
    </ScrollView>
  );
}

function Boton({ texto, onPress }) {
  return (
    <Pressable onPress={onPress} style={s.boton}>
      <Text style={s.botonTxt}>{texto}</Text>
    </Pressable>
  );
}

function TarjetaPrediccion({ p }) {
  const color = p.direccion === 'ARRIBA' ? VERDE : ROJO;
  return (
    <View style={s.tarjeta}>
      <Text style={s.txt}>{p.mercado} · vence {p.vence_utc} UTC</Text>
      <Text style={[s.grande, { color }]}>{p.direccion} {p.probabilidad_pct}%</Text>
      <Text style={s.txt}>Convicción: {p.conviccion} · contraria {p.contraria_pct}%</Text>
      {p.avisos.map((a, i) => <Text key={i} style={s.aviso}>• {a}</Text>)}
      <Text style={s.legal}>{p.aviso_legal}</Text>
    </View>
  );
}

function TarjetaAnalisis({ a }) {
  // `texto` ya viene listo en español; la app solo lo pinta. Si prefieres
  // diseñar tu propia tarjeta, todos los campos están en a.plan y a.radar.
  return (
    <View style={s.tarjeta}>
      <Text style={s.txt}>{a.texto}</Text>
      <Text style={[s.sub, { marginTop: 12 }]}>Radar de motores</Text>
      {a.radar.map((r) => (
        <Text key={r.motor} style={s.txt}>
          {r.voto === 'ALCISTA' ? '🟢' : r.voto === 'BAJISTA' ? '🔴' : '⚪'} {r.motor}: {r.detalle}
        </Text>
      ))}
    </View>
  );
}

const s = StyleSheet.create({
  fondo: { flex: 1, backgroundColor: FONDO },
  titulo: { color: '#fff', fontSize: 22, fontWeight: '700', marginBottom: 12 },
  sub: { color: GRIS, fontSize: 13, marginTop: 16, marginBottom: 8 },
  fila: { flexDirection: 'row', flexWrap: 'wrap', gap: 8 },
  chip: { borderWidth: 1, borderColor: '#2a332d', borderRadius: 16, paddingVertical: 6, paddingHorizontal: 12 },
  chipTxt: { color: '#fff' },
  boton: { backgroundColor: TARJETA, borderRadius: 10, paddingVertical: 10, paddingHorizontal: 14 },
  botonTxt: { color: VERDE, fontWeight: '600' },
  tarjeta: { backgroundColor: TARJETA, borderRadius: 14, padding: 16, marginTop: 20 },
  txt: { color: '#e5e7eb', fontSize: 14, lineHeight: 20 },
  grande: { fontSize: 30, fontWeight: '800', marginVertical: 8 },
  aviso: { color: GRIS, fontSize: 12, marginTop: 4 },
  legal: { color: GRIS, fontSize: 11, marginTop: 12 },
});
