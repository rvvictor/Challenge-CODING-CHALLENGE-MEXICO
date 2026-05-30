# Aurelion

Aurelion es un bot de arbitraje de Bitcoin con arquitectura backend/frontend: backend Python con FastAPI y frontend React. Monitorea order books multi-exchange, prioriza oportunidades por score, simula ejecuciones, maneja wallets, publica estado en vivo y visualiza P&L, riesgo e infraestructura en un cockpit web.

## Highlights

- Backend Python `FastAPI` con API REST y Server-Sent Events.
- Frontend React/Vite con dashboard operativo y nueva identidad visual.
- Market data WebSocket-first con `ccxt.pro` cuando esta disponible.
- REST polling solo como fallback despues de 5 fallos WebSocket por stream.
- Reconexion WebSocket cada 2 segundos.
- Redis Pub/Sub opcional para snapshots, trades, risk events y market events.
- Arbitraje cross-exchange BTC y triangular `USDT -> BTC -> ETH -> USDT`.
- Cola de prioridad con score y dedupe de rutas equivalentes.
- Circuit breaker por volatilidad, stale data y racha de perdidas.
- Modo demo deterministico para presentaciones sin depender de APIs externas.

## Arquitectura

```text
backend/
  app/main.py                    FastAPI, endpoints, SSE, static build
  app/core/config.py             Settings y exchanges
  app/core/models.py             Dataclasses de dominio
  app/engines/market_service.py  Orquestador principal
  app/engines/arbitrage.py       Arbitraje cross-exchange
  app/engines/triangular.py      Arbitraje triangular
  app/engines/queue.py           Ranking y dedupe
  app/engines/risk.py            Circuit breaker
  app/integrations/ccxt_provider.py
  app/integrations/redis_bus.py
frontend/
  src/main.jsx                   React cockpit
  src/styles/app.css             Sistema visual
```

## Circuit Breaker

Se corrigio para que la volatilidad no congele el P&L tan seguido:

- Umbral default subio a `2.4%` dentro de `30s`.
- Requiere minimo `8` muestras antes de activar volatilidad.
- Tiene `VOLATILITY_REARM_MS=45000`, evitando re-disparos consecutivos.
- El cooldown default sigue siendo `60s`.
- Al terminar el cooldown, `snapshot.risk.paused` vuelve a `false` automaticamente.

Condiciones:

- Volatilidad: cambio BTC mayor a `MAX_VOLATILITY_PCT`.
- Perdidas: `MAX_LOSS_STREAK=5`.
- Stale data: order book sin actualizar por mas de `MAX_BOOK_AGE_MS=5000`.

## Ejecutar Local

Instalar backend:

```bash
python -m pip install -r requirements.txt
```

Instalar frontend:

```bash
npm --prefix frontend install
```

Construir frontend y levantar backend:

```bash
npm run build
npm run dev
```

Abrir:

```text
http://localhost:8000
```

Si la terminal muestra `http://0.0.0.0:8000`, no abras esa direccion en el navegador. `0.0.0.0` solo significa que el servidor escucha en todas las interfaces; la URL navegable local es `http://localhost:8000` o `http://127.0.0.1:8000`.

Para desarrollo visual con Vite:

```bash
npm run dev:web
```

## Comandos

```bash
npm run test
npm run check
npm run build
npm run start
```

## Variables

| Variable | Default | Uso |
| --- | ---: | --- |
| `PORT` | `8000` | Puerto backend |
| `MARKET_MODE` | `auto` | `auto`, `live`, `demo` |
| `EVALUATION_INTERVAL_MS` | `450` | Frecuencia del motor |
| `WS_RECONNECT_DELAY_MS` | `2000` | Espera entre reconexiones |
| `WS_FAILURE_THRESHOLD` | `5` | Fallos antes de REST fallback |
| `POLL_INTERVAL_MS` | `1200` | REST fallback interval |
| `REDIS_URL` | vacio | Redis Pub/Sub |
| `MAX_VOLATILITY_PCT` | `2.4` | Umbral volatilidad |
| `VOLATILITY_MIN_SAMPLES` | `8` | Muestras minimas |
| `VOLATILITY_REARM_MS` | `45000` | Rearm de volatilidad |
| `PAUSE_AFTER_LOSS_MS` | `60000` | Cooldown |
| `TRIANGULAR_ENABLED` | `true` | Activa triangular |
| `TRIANGULAR_QUOTE_SIZE` | `2500` | Tamano ciclo |

## API

- `GET /api/health`
- `GET /api/snapshot`
- `GET /api/config`
- `POST /api/control`
- `POST /api/reset`
- `GET /events`

## Deploy

Render:

```text
Build: pip install -r requirements.txt && npm --prefix frontend ci && npm --prefix frontend run build
Start: python -m backend.app.main
```

Docker:

```bash
docker build -t aurelion .
docker run -p 8000:8000 -e MARKET_MODE=auto aurelion
```

## Notas

- No envia ordenes reales ni usa llaves privadas.
- En `auto`, si `ccxt.pro` no esta disponible o no hay red, puede usar demo degradado para que la UI siga viva.
- Redis es bus Pub/Sub, no almacenamiento durable. Para produccion se agregaria Postgres/Timescale o Redis Streams.
