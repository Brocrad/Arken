# Arken observability stack

Mature visibility for the multi-token pool: **ops health** (now + alerts), **analytics** (long-term trends), and **logs** (troubleshooting). The bot fleet stays on the hot path; observability is layered beside it.

## Target architecture

```text
┌─────────────────────────────────────────────────────────────────────────┐
│                         OBSERVABILITY (this compose)                     │
│  Grafana ──► Prometheus, Loki, (later) Postgres analytics              │
│  Alertmanager ──► Discord / email (configure yourself)                   │
└────────▲───────────────────────▲───────────────────────▲────────────────┘
         │ scrape               │ push                  │ SQL (phase 2)
         │                      │ logs                  │
┌────────┴────────┐    ┌────────┴────────┐    ┌─────────┴──────────┐
│ node_exporter   │    │ promtail        │    │ arken-ingest       │
│ textfile: pool  │    │ docker logs     │    │ (events API)       │
│ + cAdvisor      │    │                 │    │ SQLite → Postgres  │
└────────▲────────┘    └────────▲────────┘    └─────────▲──────────┘
         │ read .prom           │                        │ POST /v1/events
┌────────┴──────────────────────┴────────────────────────┴──────────────┐
│  data/ volume — metrics/, JSON state, heartbeats, (future) analytics  │
│  arken-gondor · arken-osgiliath · … (data plane — playback & Discord)   │
└─────────────────────────────────────────────────────────────────────────┘
```

### Separation of concerns

| Layer | Purpose | Store | Dashboard folder |
|-------|---------|-------|------------------|
| **Ops / SRE** | Pool balance, heartbeats, caps, CPU/RAM, alerts | Prometheus + Loki | Grafana → *Arken / Operations* |
| **Analytics** | Plays, retention, top guilds, command audit | Event DB (phase 2) | Grafana → *Arken / Analytics* |
| **Admin** | Approve guilds, staff, pool assignment (today: Discord) | JSON control state | Grafana ops + (phase 3) admin UI |

## Phase 1 — Run now (ops health)

**On the VPS** (Linux; needs Docker socket for Promtail + cAdvisor):

```bash
# Pool bots + shared data (existing)
docker compose -f docker-compose.yml -f docker-compose.pool.yml up -d --build

# Observability stack (same host, reads ./data/metrics)
docker compose -f docker-compose.observability.yml up -d
```

Set on **one** city only (Gondor in pool compose):

- `ARKEN_METRICS_LEADER=1` → writes `data/metrics/arken_pool.prom` (single pool-wide series)
- Every city writes `data/metrics/arken_process_<city>.prom` (no duplicate pool metrics)

Open Grafana: `http://<vps>:3000` (default `admin` / `admin` — change on first login).

Datasources provisioned: **Prometheus**, **Loki**.

**Prometheus jobs:** node exporter (host + textfiles), cAdvisor (per-container CPU/mem), self-scrape.

### What you can see in phase 1

- Guilds / slots / load score per city
- Active players, at-cap, heartbeat age / alive
- Container CPU & memory (utilization for allocation decisions)
- Logs filtered to `arken-*` containers

### Alerts to add (phase 1b)

In Grafana or Alertmanager:

- `arken_token_alive == 0` or `heartbeat_age > 120`
- `arken_token_at_cap == 1` (capacity planning)
- Container memory near limit

## Phase 2 — Analytics data plane

**Do not** write plays into `token_pool.json`. Add a sidecar:

1. **`arken-ingest`** — one process, owns `analytics.db` (SQLite) or Postgres.
2. Bots **async POST** events (`play_started`, `play_completed`, `allowguild`, …).
3. Nightly **rollups** (`plays_per_guild_day`, `minutes_streamed`).
4. Grafana **PostgreSQL/SQLite** datasource for trend panels (30d / 90d / 1y).

Postgres is the usual ops upgrade when you want concurrent readers and backups; SQLite is fine for single-VPS ingest.

## Phase 3 — Admin dashboard

Read-only first:

- Grafana tables from Prometheus + SQL (guild list, assignments, staff count).
- Optional **read-only API** over `allowed_guilds.json` / `token_pool.json`.

Write path (approve guild, whitelist) stays Discord until you add a state service with auth — do not give Grafana write access to JSON on disk.

## Resource allocation

Use phase 1 metrics together:

| Signal | Metric | Decision |
|--------|--------|----------|
| City saturated | `arken_token_at_cap`, `slots_free` | Invite next city or raise `ARKEN_MAX_GUILDS_PER_TOKEN` |
| Hot city | `arken_token_load_score`, `active_players` | `--auto` already balances; confirm heartbeats |
| Underused city | low `guilds` + spare slots | Prefer assign to quiet token |
| Node pressure | cAdvisor CPU/mem | Add VPS resources or split cities across hosts |

## Files

| Path | Role |
|------|------|
| `docker-compose.observability.yml` | Prometheus, Grafana, Loki, Promtail, exporters |
| `deploy/observability/prometheus/prometheus.yml` | Scrape config |
| `deploy/observability/grafana/provisioning/` | Datasources (and dashboards later) |
| `deploy/observability/promtail/promtail.yml` | Docker log shipping |
| `data/metrics/arken_pool.prom` | Pool gauges (leader only) |
| `data/metrics/arken_process_*.prom` | Per-city process gauges |

## Security

- Do not expose Prometheus/Loki publicly without auth/Tailscale.
- Grafana behind reverse proxy + SSO or strong password.
- Analytics ingest API: shared secret on internal Docker network only.

## Related env vars

See `.env.example`: `ARKEN_METRICS`, `ARKEN_METRICS_LEADER`, `ARKEN_POOL_HEARTBEAT_MAX`, pool weights.