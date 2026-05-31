<div align="center">

# Aurelion

### Inteligencia de arbitraje de Bitcoin para el CODING CHALLENGE MEXICO

Creado por **Victor Ruiz**

[![Python](https://img.shields.io/badge/Python-3.11+-1f6feb?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-Backend-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![React](https://img.shields.io/badge/React-Interfaz-149eca?style=for-the-badge&logo=react&logoColor=white)](https://react.dev/)
[![Vite](https://img.shields.io/badge/Vite-Build-646cff?style=for-the-badge&logo=vite&logoColor=white)](https://vite.dev/)
[![Redis](https://img.shields.io/badge/Redis-PubSub-dc382d?style=for-the-badge&logo=redis&logoColor=white)](https://redis.io/)
[![Postgres](https://img.shields.io/badge/Postgres-Auditoría-4169e1?style=for-the-badge&logo=postgresql&logoColor=white)](https://www.postgresql.org/)

</div>

---

## Descripción

**Aurelion** es un bot de arbitraje de Bitcoin construido con una arquitectura real de backend y frontend. El sistema monitorea libros de órdenes en múltiples exchanges, detecta oportunidades de arbitraje cross-exchange, triangular y ciclos dinámicos de 4 pasos, prioriza las mejores rutas con una cola basada en valor esperado, simula ejecuciones con costos realistas y muestra todo el proceso en un dashboard web claro, visual y auditable.

El objetivo del proyecto no es fingir que cualquier spread es ejecutable. Aurelion modela la cadena completa de decisión:

```text
datos de mercado -> libros normalizados -> motores de arbitraje -> score de valor esperado
-> cola de prioridad -> compuertas de riesgo -> validación de inventario
-> ejecución simulada -> auditoría durable -> dashboard en vivo
```

El proyecto fue diseñado para demostrar dominio técnico bajo presión: velocidad, razonamiento financiero, manejo de fallas, claridad visual, trazabilidad y código limpio.

Repositorio:

```text
https://github.com/rvvictor/Challenge-CODING-CHALLENGE-MEXICO
```

---

## Capturas de pantalla

### Vista principal del cockpit

![Vista principal de Aurelion](docs/screenshots/dashboard-overview.png)

### Vista operativa extendida

![Vista extendida de Aurelion](docs/screenshots/dashboard-wide.png)

---

## Por qué Aurelion destaca

| Área | Qué hace Aurelion |
| --- | --- |
| Datos de mercado | Usa una estrategia **WebSocket-first** con `ccxt.pro` cuando está disponible. El polling REST solo se activa como respaldo después de fallas repetidas. |
| Robustez | Reintenta conexiones cada 2 segundos, activa REST después de 5 fallas de WebSocket y puede deshabilitar streams dañados sin romper el sistema. |
| Velocidad | El perfil por defecto opera con 5 exchanges rápidos para reducir latencia, pero mantiene un catálogo de 10 exchanges para cobertura. |
| Arbitraje | Detecta arbitraje cross-exchange de BTC, ciclos triangulares clásicos y ciclos dinámicos de 4 pasos como `USDT -> BTC -> ETH -> SOL -> USDT`. |
| Calidad de ejecución | Modela comisiones, slippage, impacto por retiros, riesgo de latencia, movimiento adverso de precio y penalización por inventario. |
| Priorización | La cola deduplica rutas equivalentes y ordena por valor esperado, confianza, liquidez y riesgo ajustado. |
| Riesgo | Incluye circuit breaker por volatilidad, datos stale, rachas de pérdidas y presupuesto de riesgo por hora. |
| Auditoría | Mantiene historial en memoria y persistencia opcional con Postgres o SQLite local. |
| Interfaz | Dashboard enfocado en P&L, velocidad, salud de exchanges, decisión actual, mercado vivo, señales y trades ejecutados. |
| Demo realista | Simulador determinístico con shocks controlados para mostrar trades normales, parciales, triangulares y dinámicos sin ganancias absurdas. |

---

## Stack tecnológico

| Capa | Tecnología |
| --- | --- |
| Backend API | Python, FastAPI, Uvicorn |
| Datos de mercado | `ccxt.pro` para WebSockets cuando está instalado, `ccxt.async_support` para respaldo REST |
| Frontend | React 19, Vite, lucide-react |
| Tiempo real | Server-Sent Events con snapshot REST como respaldo |
| Mensajería | Redis Pub/Sub opcional |
| Persistencia | Postgres mediante `DATABASE_URL`, SQLite local como respaldo |
| Pruebas | `unittest` en Python, pruebas de motores y verificación de build del frontend |
| Despliegue | Railway |

---

## Funciones principales

### 1. Datos de mercado WebSocket-first

Aurelion intenta usar `ccxt.pro` para recibir libros de órdenes en vivo por WebSocket. Si `ccxt.pro` no está disponible, el sistema puede operar con `ccxt.async_support` como respaldo REST cuando sea posible.

Cada stream mantiene su propio estado:

- `websocket`: modo principal.
- `rest`: modo de respaldo, activado solo después de 5 fallas consecutivas.
- `disabled`: estado seguro cuando REST también falla repetidamente.
- `healthScore`: puntaje automático que baja cuando hay errores o latencia alta.

El proveedor también usa límites seguros por exchange para evitar errores conocidos en KuCoin, Kraken, Bybit y Bitfinex cuando rechazan profundidades no soportadas.

### 2. Motores de arbitraje

Aurelion detecta:

- **Arbitraje cross-exchange de BTC**: comprar BTC en un exchange y venderlo en otro.
- **Arbitraje triangular clásico**: por ejemplo `USDT -> BTC -> ETH -> USDT`.
- **Ciclos dinámicos de 4 pasos**: por ejemplo `USDT -> BTC -> ETH -> SOL -> USDT`.
- **Near misses**: señales no ejecutables que explican qué tan cerca estuvo el mercado de ser rentable.

Cada oportunidad incluye edge neto, edge bruto, comisiones, slippage, riesgo de latencia, valor esperado, confianza, ratio de llenado y decisión de ejecución.

### 3. Cola de prioridad por valor esperado

La cola no ordena solamente por spread bruto. Aurelion calcula una aproximación de valor esperado:

```text
EV = utilidad_neta * confianza - riesgo_latencia - riesgo_volatilidad - penalización_inventario
```

También elimina duplicados:

- Si aparecen `Binance -> Kraken` y `Kraken -> Binance` en el mismo tick, conserva la ruta con mejor score ajustado.
- Si aparecen varias versiones de un mismo ciclo triangular, conserva la de mayor valor esperado.
- Las oportunidades rentables tienen prioridad sobre señales rechazadas o bloqueadas.

### 4. Simulador de ejecución

La capa de ejecución simula:

- fills completos;
- fills parciales;
- ciclos triangulares parciales;
- movimiento adverso de precio por latencia;
- rebalanceo virtual de inventario;
- P&L realizado;
- metadatos completos para dashboard y exportación.

El modo demo está calibrado para mostrar trades cross-exchange normales, parciales normales, oportunidades triangulares y señales dinámicas de 4 pasos sin producir resultados irreales.

### 5. Circuit breaker y control de riesgo

Aurelion pausa la ejecución cuando el riesgo deja de ser aceptable:

- shock de volatilidad en BTC dentro de la ventana configurada;
- 5 trades negativos consecutivos;
- libros de órdenes sin actualizar;
- presupuesto de pérdidas por hora excedido;
- prueba manual de volatilidad desde el dashboard.

Cuando el circuit breaker está activo, el sistema sigue observando el mercado, pero deja de ejecutar nuevos trades. Después del cooldown, se reactiva automáticamente.

### 6. Auditoría y trazabilidad

El runtime conserva eventos importantes de la sesión:

- oportunidades detectadas;
- trades ejecutados;
- eventos de riesgo;
- fallas de streams de mercado;
- serie de P&L;
- ledger de replay;
- exportación completa en JSON.

Si se configura `DATABASE_URL`, Aurelion escribe registros durables en Postgres. Si no hay Postgres, usa SQLite local para que el proyecto siga siendo fácil de correr durante la evaluación.

---

## Estructura del proyecto

```text
backend/
  app/
    main.py                         FastAPI, SSE, API de control, exportación y SPA
    core/
      config.py                     Configuración, catálogo de exchanges y perfiles
      models.py                     Modelos de dominio compatibles con Pydantic
    engines/
      market_service.py             Orquestador principal del motor
      arbitrage.py                  Motor de arbitraje cross-exchange
      triangular.py                 Motor triangular y ciclos dinámicos multi-leg
      queue.py                      Cola de prioridad y deduplicación
      execution.py                  Ejecución simulada y movimiento adverso
      ledger.py                     Wallets, P&L realizado/no realizado y exposición
      risk.py                       Circuit breaker y presupuesto de riesgo
      simulator.py                  Mercado demo determinístico con shocks controlados
      edge_analysis.py              Explicabilidad, SLO de latencia y calidad demo
      edge_ledger.py                Ledger de decisiones reproducible
      event_store.py                Historial en memoria con persistencia opcional
    integrations/
      ccxt_provider.py              Proveedor WebSocket-first con respaldo REST
      redis_bus.py                  Pub/Sub opcional
      persistence.py                Persistencia durable en Postgres o SQLite
      global_market.py              Contexto externo de BTC/ETH
    tests/
      test_engines.py               Pruebas de motores, riesgo, proveedor, métricas y API

frontend/
  src/
    main.jsx                        Cockpit React
    styles/app.css                  Sistema visual y layout responsivo

docs/
  screenshots/                      Capturas usadas en este README
```

---

## Instalación

### Requisitos

- Python 3.11+
- Node.js 20+
- Redis opcional
- Postgres opcional
- `ccxt.pro` opcional para streams WebSocket reales de exchanges

### 1. Clonar el repositorio

```bash
git clone https://github.com/rvvictor/Challenge-CODING-CHALLENGE-MEXICO.git
cd Challenge-CODING-CHALLENGE-MEXICO
```

### 2. Instalar dependencias del backend

```bash
python -m pip install -r requirements.txt
```

### 3. Instalar dependencias del frontend

```bash
npm --prefix frontend install
```

### 4. Construir el frontend

```bash
npm run build
```

### 5. Ejecutar la aplicación

```bash
npm run dev
```

Abrir:

```text
http://localhost:8000
```

Si la terminal muestra `http://0.0.0.0:8000`, se debe abrir `http://localhost:8000` en el navegador. `0.0.0.0` solo significa que el servidor escucha en todas las interfaces; no es una URL navegable local.

---

## Comandos disponibles

```bash
npm run dev       # Ejecuta FastAPI y sirve el frontend construido
npm run start     # Comando de inicio estilo producción
npm run build     # Construye el frontend React
npm run check     # Compila backend y construye frontend
npm run test      # Ejecuta pruebas del backend
npm run dev:web   # Servidor Vite solo para desarrollo visual
```

---

## Modos de ejecución

| Modo | Propósito |
| --- | --- |
| `demo` | Simulador determinístico. Es el modo recomendado para evaluación visual porque muestra señales realistas rápidamente sin depender de APIs externas. |
| `auto` | Intenta conectarse a datos reales de mercado; si el entorno bloquea exchanges o falta `ccxt.pro`, puede degradarse de forma segura. |
| `live` | Ruta de datos en vivo para validar mercado real. La ejecución sigue siendo paper/simulada. |

### Aclaración importante sobre `auto` y `live`

En `auto` y `live` es normal que haya menos movimiento que en `demo`. En mercado real, las oportunidades netas duran muy poco y muchas quedan descartadas al descontar comisiones, slippage, latencia, liquidez e inventario. Además, este proyecto no incluye API keys, llaves privadas ni permisos para ejecutar trades reales con dinero. Aurelion está preparado como bot de análisis, simulación y paper trading para el challenge.

---

## Perfiles de exchanges

| Perfil | Exchanges | Uso |
| --- | --- | --- |
| `speed` | OKX, Bybit, KuCoin, Kraken, Bitstamp | Perfil por defecto. Menor latencia y evaluación más limpia. |
| `demo` | OKX, Bybit, KuCoin, Kraken, Bitstamp | Perfil controlado para el simulador. |
| `coverage` | 10 exchanges configurados | Mayor cobertura para demostrar universo global. |

Catálogo completo configurado:

```text
Binance, OKX, Kraken, Coinbase, Bitstamp, Bybit, KuCoin, Gate.io, Bitfinex, Gemini
```

---

## Variables de entorno

| Variable | Valor por defecto | Descripción |
| --- | --- | --- |
| `PORT` | `8000` | Puerto del backend. |
| `MARKET_MODE` | `auto` | Modo `demo`, `auto` o `live`. |
| `EXCHANGE_PROFILE` | `speed` | Perfil `speed`, `demo` o `coverage`. |
| `ACTIVE_EXCHANGES` | vacío | Lista separada por comas. Usar `all` para activar el catálogo completo. |
| `EVALUATION_INTERVAL_MS` | `450` | Intervalo de evaluación del motor. |
| `ORDER_BOOK_LIMIT` | `20` | Profundidad base del libro. Algunos exchanges usan límites seguros propios. |
| `WS_RECONNECT_DELAY_MS` | `2000` | Espera antes de reconectar un WebSocket fallido. |
| `WS_FAILURE_THRESHOLD` | `5` | Fallas consecutivas antes de activar REST fallback. |
| `POLL_INTERVAL_MS` | `1200` | Frecuencia del polling REST cuando está activo. |
| `REST_RECOVERY_ATTEMPT_MS` | `60000` | Tiempo antes de intentar volver de REST a WebSocket. |
| `MIN_TRADE_BTC` | `0.002` | Tamaño mínimo ejecutable en BTC. |
| `MAX_TRADE_BTC` | `0.015` | Tamaño máximo simulado por trade. |
| `MIN_NET_BPS` | `0.75` | Edge neto mínimo para arbitraje cross-exchange. |
| `TRIANGULAR_ENABLED` | `true` | Activa detección triangular y ciclos dinámicos. |
| `TRIANGULAR_QUOTE_SIZE` | `650` | Notional inicial para evaluar ciclos. |
| `DEMO_MIN_EXECUTION_GAP_MS` | `22000` | Separación mínima entre fills simulados en demo. |
| `MAX_VOLATILITY_PCT` | `2.4` | Umbral de volatilidad para circuit breaker. |
| `VOLATILITY_MIN_SAMPLES` | `8` | Muestras mínimas antes de activar pausa por volatilidad. |
| `PAUSE_AFTER_LOSS_MS` | `60000` | Cooldown del sistema de riesgo. |
| `RISK_BUDGET_HOUR_USD` | `75` | Presupuesto de pérdida por hora antes de pausar. |
| `REDIS_URL` | vacío | Activa Redis Pub/Sub cuando se proporciona. |
| `DATABASE_URL` | vacío | Activa persistencia durable en Postgres cuando se proporciona. |
| `PERSISTENCE_ENABLED` | `true` | Activa Postgres o SQLite como almacén de eventos. |

---

## API

| Endpoint | Descripción |
| --- | --- |
| `GET /api/health` | Salud del runtime. |
| `GET /api/snapshot` | Snapshot completo para el dashboard. |
| `GET /api/metrics` | Métricas operativas compactas. |
| `GET /metrics` | Métricas estilo Prometheus en texto. |
| `GET /api/config` | Configuración activa y catálogo de exchanges. |
| `GET /api/export/session` | Exportación completa de sesión para revisión. |
| `GET /api/replay` | Eventos del ledger reproducible. |
| `POST /api/control` | Cambia modo, ejecución, exchanges activos o activa stress de volatilidad. |
| `POST /api/reset` | Reinicia la sesión runtime. |
| `GET /events` | Stream SSE para actualizaciones en vivo del dashboard. |

---

## Guía rápida del dashboard

| Sección | Cómo interpretarla |
| --- | --- |
| `Realized P&L` | Ganancia capturada por ejecuciones simuladas después de costos modelados. |
| `Bot Status` | Indica si el bot está corriendo, pausado por riesgo o con ejecución manual apagada. |
| `Best Opportunity` | Mejor edge neto actual después de comisiones, slippage, latencia y riesgo. |
| `Detected Signals` | Total de oportunidades retenidas en el historial de la sesión. |
| `Speed` | Frescura de libros y p95 de edad del book para evaluar latencia. |
| `Data Health` | Venues activos, salud de streams y exchanges degradados. |
| `Live Market` | Bid, ask y profundidad por exchange. |
| `Current Decision` | Explica por qué la oportunidad principal se ejecuta, se rechaza o se bloquea. |
| `Real Costs` | Compara rentabilidad prefunded contra rentabilidad con costos de settlement. |
| `Priority Queue` | Oportunidades rankeadas después de deduplicación y score por valor esperado. |
| `Signal History` | Historial de señales con filtros para Cross, Partial Cross, Triangular y Dynamic 4-leg. |
| `Executed Trades` | Fills ejecutados con tipo, hora, P&L, EV y estado parcial/completo. |
| `Exchanges` | Selección visual de exchanges activos, con máximo de 5 para mantener baja latencia. |
| `Wallets` | Saldos simulados y exposición por venue. |
| `System / Infra` | Redis, base de datos, streams, timeline de riesgo y estado de auditoría. |

---

## Balance del modo demo

El modo demo es determinístico y está controlado para presentaciones. Genera:

- shocks cross-exchange de BTC;
- situaciones de liquidez parcial;
- edges triangulares clásicos;
- señales ocasionales de ciclos dinámicos de 4 pasos;
- eventos de volatilidad para validar el circuit breaker.

El simulador evita dos extremos:

1. **Demasiado quieto**: los jueces tendrían que esperar mucho para ver actividad.
2. **Demasiado falso**: ganancias enormes en pocos minutos reducirían credibilidad.

El comportamiento actual busca una cadencia legible: pocos trades por minuto, P&L moderado, fills parciales visibles y una mezcla entre oportunidades normales y ciclos.

---

## Pruebas

Ejecutar:

```bash
npm run test
npm run check
```

La suite cubre:

- estimación de fills en múltiples niveles del order book;
- fills parciales;
- deduplicación de rutas cross-exchange;
- rentabilidad triangular;
- detección de ciclos dinámicos de 4 pasos;
- balance demo entre señales cross, parciales y cíclicas;
- reactivación del circuit breaker;
- botón de stress de volatilidad;
- límites seguros de order book por exchange;
- puntaje automático de salud del proveedor;
- selección de perfiles de exchanges;
- cambio de exchanges sin perder P&L;
- rebalanceo de inventario;
- ordenamiento por valor esperado;
- endpoint de métricas cuando FastAPI TestClient está disponible;
- exportación de sesión y replay ledger.

---

## Despliegue en Railway

El despliegue final está pensado para **Railway**. La aplicación ya está preparada para correr como servicio web Python que construye el frontend React y sirve la SPA desde FastAPI.

Configuración recomendada para Railway:

```text
Repositorio: https://github.com/rvvictor/Challenge-CODING-CHALLENGE-MEXICO
Comando de build: pip install -r requirements.txt && npm --prefix frontend ci && npm --prefix frontend run build
Comando de inicio: python -m backend.app.main
```

Variables recomendadas:

```text
MARKET_MODE=auto
AUTO_EXECUTION=true
EXCHANGE_PROFILE=speed
```

Variables opcionales:

```text
DATABASE_URL=<url-de-postgres-en-railway>
REDIS_URL=<url-de-redis-si-se-configura>
```

Para evaluación local o demo rápida, también se puede iniciar con:

```text
MARKET_MODE=demo
```

---

## Notas importantes

- Aurelion es un sistema de análisis, simulación y paper trading para el challenge.
- No envía órdenes reales ni opera dinero real.
- No incluye API keys, llaves privadas ni permisos de exchanges.
- `ccxt.pro` se usa cuando está disponible para WebSockets reales; `ccxt` abierto funciona como base para REST fallback.
- Redis es opcional. El dashboard funciona con Server-Sent Events incluso sin Redis.
- Postgres es opcional. Si `DATABASE_URL` no está configurado, Aurelion puede persistir eventos localmente con SQLite.
- El modo demo es la ruta recomendada para jueces porque es determinística, visual e independiente de bloqueos de red o restricciones de exchanges.
- En `auto` y `live` es normal ver menos trades rentables porque el mercado real es más competitivo y los costos eliminan muchas oportunidades.

---

## Autor

**Victor Ruiz**  
Proyecto para **CODING CHALLENGE MEXICO**
