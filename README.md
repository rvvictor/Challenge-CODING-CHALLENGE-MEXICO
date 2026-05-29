# Bitcoin Arbitrage Sentinel

Bitcoin Arbitrage Sentinel es una web app de monitoreo y simulacion de arbitraje BTC entre exchanges. El sistema escucha order books por WebSocket con CCXT/CCXT Pro, cae a REST polling solo despues de fallos repetidos, calcula oportunidades netas, simula ejecuciones con liquidez parcial, mantiene wallets prefundeadas y muestra P&L en tiempo real.

La app esta pensada para evaluacion tecnica: usa dependencias reales (`ccxt`, `ioredis`), puede conectarse a datos de mercado reales cuando hay red, soporta Redis Pub/Sub y conserva modo demo deterministico para que la presentacion siempre funcione.

## Que resuelve

- Monitoreo WebSocket-first de books BTC en Binance, OKX, Kraken, Coinbase y Bitstamp.
- Reconexion WebSocket cada 2 segundos; tras 5 fallos por stream activa REST polling.
- Deteccion de rutas `buy ask` menor que `sell bid` entre exchanges.
- Motor de arbitraje triangular por exchange para ciclos como `USDT -> BTC -> ETH -> USDT`.
- Cola de prioridad con score, ranking y deduplicacion de rutas equivalentes.
- Calculo neto con trading fees, slippage estimado, latencia y reserva de rebalanceo por withdrawal fees.
- Simulacion de ejecucion respetando profundidad de book, ordenes parciales y balances por wallet.
- Circuit breaker por volatilidad, stale data, racha de perdidas y switch de autoejecucion.
- Redis Pub/Sub opcional para publicar snapshots, trades, eventos de mercado y circuit breaker.
- Dashboard web con books, streams, queue de oportunidades, trades ejecutados, wallets, matriz de spreads, eventos de riesgo y P&L.

## Arquitectura

```text
public/                 UI HTML/CSS/JS, SSE client, canvas charts
src/server.js           HTTP server, static files, REST API, SSE stream
src/exchanges/          CCXT stream provider, adaptadores REST fallback y simulador
src/integrations/       Redis Pub/Sub opcional
src/engine/fills.js     Consumo de niveles y fills parciales
src/engine/arbitrage*   Evaluacion cross-exchange neta
src/engine/triangular*  Arbitraje triangular por exchange
src/engine/opportunity* Cola de prioridad con dedupe
src/engine/execution*   Simulador de ejecucion y cooldowns
src/engine/wallet*      Ledger de balances BTC/ETH/USDT
src/engine/risk*        Circuit breaker por volatilidad, stale data y perdidas
src/storage/            Historial en memoria para oportunidades, trades, eventos y P&L
tests/                  Pruebas unitarias e integracion
```

Flujo principal:

1. `CcxtStreamProvider` abre `watchOrderBook` por exchange/simbolo y normaliza los books.
2. Si un stream WebSocket falla, espera `WS_RECONNECT_DELAY_MS`; al acumular `WS_FAILURE_THRESHOLD` fallos activa REST polling para ese stream.
3. `MarketDataEngine` evalua varias veces por segundo los ultimos books recibidos.
4. `ArbitrageEngine` compara rutas cross-exchange y `TriangularArbitrageEngine` evalua ciclos intra-exchange.
5. `OpportunityQueue` filtra duplicados y ordena por score; solo lo mejor pasa a ejecucion simulada.
6. `RiskManager` puede pausar el bot por volatilidad, stale data o 5 perdidas consecutivas.
7. `WalletLedger` actualiza balances y publica snapshots por SSE; si Redis esta activo tambien publica por Pub/Sub.

## Modelo de rentabilidad

Para cada ruta cross-exchange se calcula:

```text
grossProfit = sellQuote - buyQuote
totalCosts = buyFee + sellFee + slippageBuy + slippageSell + latencyRisk + rebalanceReserve
netProfit = grossProfit - totalCosts
netBps = netProfit / buyQuote * 10000
```

Para triangular:

```text
quoteIn -> buy BTC -> buy ETH -> sell ETH -> quoteOut
netProfit = quoteOut - quoteIn - latencyRisk
netBps = netProfit / quoteIn * 10000
```

Detalles relevantes:

- Las ordenes se llenan atravesando niveles del order book, no solo top-of-book.
- El volumen se limita por profundidad disponible, BTC disponible en el exchange vendedor y USDT disponible en el comprador.
- `rebalanceReserve` modela el costo eventual de reequilibrar inventario entre exchanges.
- `latencyRisk` aumenta con la latencia medida de los books involucrados.
- El bot aplica cooldown por ruta para no sobreoperar la misma divergencia.

## Cola de prioridad y dedupe

Cada tick puede generar muchas oportunidades. Antes de ejecutar:

- Las rutas simples se agrupan por par no dirigido de exchanges. Si aparecen `Binance -> Kraken` y `Kraken -> Binance` en el mismo tick, conserva solo la de mayor score.
- Las rutas triangulares se agrupan por `exchange + cycleId`. Si el mismo ciclo aparece duplicado, conserva solo el mejor score.
- La cola se ordena por score ajustado por edge neto, confianza, tamano y latencia.

## Circuit breaker

El bot deja de ejecutar nuevas operaciones cuando se activa cualquiera de estas condiciones:

- Volatilidad: BTC cambia mas de `1.5%` dentro de `30s`.
- Perdidas consecutivas: `5` trades negativos seguidos.
- Stale data: algun order book principal no se actualiza en mas de `5s`.

Cuando se activa:

- No ejecuta nuevos trades.
- Registra un evento con timestamp, motivo y metadata.
- Publica el evento en Redis Pub/Sub si `REDIS_URL` esta configurado.
- Se reactiva automaticamente despues de `60s`.

## Modos de mercado

- `auto` default: usa WebSockets reales si `ccxt` esta disponible; si falta la dependencia o no hay datos, usa demo degradado para que la UI siga viva.
- `live`: solo datos reales por WebSocket/REST fallback. No genera demo.
- `demo`: todos los books son simulados con shocks controlados para mostrar arbitraje.

La UI permite cambiar modo sin reiniciar el proceso.

## Ejecutar localmente

Requisitos: Node.js 20 o superior.

```bash
npm install
npm run dev
```

Abrir:

```text
http://localhost:3000
```

Tambien se puede ejecutar directamente:

```bash
node src/server.js
```

## Pruebas y checks

```bash
npm run check
npm test
```

## Variables de entorno

| Variable | Default | Uso |
| --- | ---: | --- |
| `PORT` | `3000` | Puerto HTTP |
| `MARKET_MODE` | `auto` | `auto`, `live` o `demo` |
| `EVALUATION_INTERVAL_MS` | `450` | Frecuencia de evaluacion del motor |
| `POLL_INTERVAL_MS` | `1200` | Intervalo REST fallback |
| `REQUEST_TIMEOUT_MS` | `2500` | Timeout por API externa |
| `WS_RECONNECT_DELAY_MS` | `2000` | Espera entre reconexiones WebSocket |
| `WS_FAILURE_THRESHOLD` | `5` | Fallos antes de activar REST fallback |
| `REDIS_URL` | vacio | URL Redis para Pub/Sub |
| `REDIS_ENABLED` | auto | Fuerza Redis on/off |
| `TRIANGULAR_ENABLED` | `true` | Activa motor triangular |
| `TRIANGULAR_QUOTE_SIZE` | `2500` | Tamano base del ciclo triangular |
| `AUTO_EXECUTION` | `true` | Activa ejecucion simulada |
| `MIN_TRADE_BTC` | `0.004` | Tamano minimo |
| `MAX_TRADE_BTC` | `0.09` | Tamano maximo por trade |
| `MIN_NET_PROFIT_USD` | `0.75` | Ganancia neta minima |
| `MIN_NET_BPS` | `1.25` | Edge neto minimo |
| `WITHDRAWAL_FEE_IMPACT` | `0.18` | Fraccion de withdrawal cost cargada al trade |
| `MAX_VOLATILITY_PCT` | `1.5` | Umbral del circuit breaker |
| `VOLATILITY_WINDOW_MS` | `30000` | Ventana de volatilidad |
| `MAX_LOSS_STREAK` | `5` | Perdidas seguidas antes de pausar |
| `PAUSE_AFTER_LOSS_MS` | `60000` | Cooldown del circuit breaker |

## API

- `GET /api/health`: estado basico.
- `GET /api/snapshot`: snapshot completo del motor.
- `GET /api/config`: parametros visibles de exchanges/riesgo.
- `POST /api/control`: cambia `mode` o `autoExecution`.
- `POST /api/reset`: reinicia historial, wallets y P&L.
- `GET /events`: stream SSE para la interfaz.

## Redis Pub/Sub

Si `REDIS_URL` esta configurado, la app publica:

- `btc-arb:snapshots`
- `btc-arb:trades`
- `btc-arb:risk`
- `btc-arb:market-events`

El namespace se cambia con `REDIS_NAMESPACE`.

## Despliegue

### Render

1. Crear un Web Service desde el repositorio.
2. Build command: `npm install`
3. Start command: `npm start`
4. Environment: `MARKET_MODE=auto`
5. Opcional: agregar Redis y configurar `REDIS_URL`

### Railway

Railway detecta Node automaticamente. Configurar:

```text
Start command: npm start
MARKET_MODE=auto
REDIS_URL=redis://...
```

### Docker / Cloud Run / Fly.io

```bash
docker build -t bitcoin-arbitrage-sentinel .
docker run -p 3000:3000 -e MARKET_MODE=auto bitcoin-arbitrage-sentinel
```

## Limitaciones conscientes

- Este proyecto no envia ordenes reales ni usa llaves privadas. Es un simulador de ejecucion para el challenge.
- REST polling existe solo como fallback por stream cuando WebSocket falla 5 veces.
- Coinbase y Bitstamp reportan BTC/USD, mientras Binance/OKX/Kraken usan BTC/USDT. Para la demo se tratan como USD-equivalentes; una version productiva agregaria FX/USDT basis.
- El historial durable sigue en memoria para mantener el despliegue simple. Redis se usa como bus Pub/Sub; para produccion se agregaria Postgres/Timescale o Redis Streams.
