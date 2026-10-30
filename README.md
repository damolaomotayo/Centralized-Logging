# Project 3 — Centralized Logging with Loki, Promtail and Grafana

Three small services write structured logs. **Promtail** collects them (plus the EC2 host's
SSH/auth log), **Loki** stores and indexes them, and **Grafana** shows dashboards, lets you
search and follow a single request across services, and fires **log-based alerts** to a
webhook "alert inbox". Buttons on a control panel break things on purpose (payments outage,
slow payments, brute-force login attack) so you can watch the logs, dashboards and alerts react.

Everything runs with Docker Compose on **one EC2 instance** and is opened from your browser at
`http://<EC2-PUBLIC-IP>:<port>`.

## Architecture

```
 Your laptop (browser)                         EC2 m7i-flex.large · Ubuntu 24.04 · Docker Compose
 ─────────────────────                         ─────────────────────────────────────────────────────────
                         Security group:        ┌───────────────────── docker network ──────────────────┐
  :8080 control panel ──► 22, 3000, 8080, 5001  │                                                        │
  :3000 Grafana       ──►  from "My IP" only ──►│  shop-api :8080 ──HTTP──► payments :9000 (internal)    │
  :5001 alert inbox   ──►                       │     │  (generates traffic)       │                      │
                                                │     ▼ logfmt lines               ▼                      │
                                                │  ┌─────────── volume app-logs: /var/log/app/*.log ───┐ │
                                                │  └──────────────────────┬────────────────────────────┘ │
                                                │  host /var/log/auth.log ┤ (read-only mount)            │
                                                │                         ▼                               │
                                                │                  Promtail  (tail, parse, label)         │
                                                │                         │ push                          │
                                                │                         ▼                               │
                                                │                  Loki :3100 (index labels, store chunks)│
                                                │                         ▲ LogQL                         │
                                                │                         │                               │
                                                │  Grafana :3000 ── dashboards, Explore, alert rules      │
                                                │        │  every 10 s: evaluate log queries              │
                                                │        └──webhook──► alert-receiver :5001 (inbox page)  │
                                                └────────────────────────────────────────────────────────┘
       Loki :3100 and Promtail :9080 are bound to 127.0.0.1 — used by the scripts on the instance only.
```

## What's in it

| Component | Image | Port (where) | Memory limit | Role |
|---|---|---|---|---|
| Loki | `grafana/loki:3.4.2` | 3100 (127.0.0.1) | 1 GiB | Log database: indexes labels only, stores compressed chunks on disk, 72 h retention |
| Promtail | `grafana/promtail:3.4.2` | 9080 (127.0.0.1) | 256 MiB | Tails `/var/log/app/*.log` and host `auth.log`/`syslog`, parses logfmt, adds labels, pushes to Loki |
| Grafana | `grafana/grafana:11.6.1` | 3000 (public) | 512 MiB | Provisioned datasource, dashboard, 3 alert rules, webhook contact point |
| shop-api | built from `python:3.12-slim` | 8080 (public) | 128 MiB | Fake shop + control panel; generates traffic; calls payments |
| payments | same image | 9000 (internal) | 128 MiB | Fake payment service (can fail or be slow) |
| alert-receiver | same image | 5001 (public) | 128 MiB | Receives Grafana webhooks and lists alerts on a page |

Total limits ≈ 2.1 GiB (real use is usually under 1 GiB). Not tight on 8 GiB; a 4 GiB instance would also work.

**Alerts (Grafana-managed, evaluated every 10 s against Loki)**

| Alert | LogQL (simplified) | Fires when |
|---|---|---|
| HighErrorRate | `sum(count_over_time({job="apps", level="ERROR"}[1m]))` | > 5 ERROR lines/min for 30 s |
| BruteForceLogin | `sum by (src_ip) (count_over_time({service="shop-api"} \| logfmt \| event="login_failed" [1m]))` | > 10 failed logins/min from one IP (one alert per IP) |
| SlowCheckout | `quantile_over_time(0.95, … \| unwrap duration_ms [2m])` | checkout p95 > 1000 ms for 30 s |

## Key ideas to explain

1. **Centralized logging** — logs from every service and the host land in one searchable place, instead of `ssh` + `grep` on each box.
2. **Structured logs (logfmt)** — every line is `key=value` pairs (`level=ERROR service=payments trace_id=… status=503`), so queries can filter and count fields.
3. **Labels vs. parsed fields (cardinality)** — Loki indexes only labels. We label just `job`, `service`, `level` (few values). High-cardinality fields like `trace_id`, `user`, `src_ip` stay in the line and are parsed at query time with `| logfmt`. Too many label values = slow, expensive Loki.
4. **Metrics from logs** — LogQL turns lines into numbers: `count_over_time` (rates), `unwrap duration_ms` + `quantile_over_time` (latency p95). No separate metrics system needed for these.
5. **Correlation with trace_id** — the shop passes a `trace_id` to payments. A Grafana "derived field" turns it into a link that shows that request's lines from every service.
6. **Log-based alerting** — Grafana runs LogQL on a schedule; query → reduce → threshold → `for` duration → notification policy → contact point (webhook).
7. **Security logging** — host `auth.log` (SSH logins, sudo) is shipped too; brute-force detection is just a LogQL count grouped by source IP. Passwords are never logged.
8. **Retention** — Loki's compactor deletes logs older than 72 h, so the disk doesn't fill.

## Repo layout

```
.
├── docker-compose.yml          # all 6 services, memory limits, port bindings
├── .env.example                # copied to .env by the scripts (random Grafana password, public IP, image overrides)
├── app/                        # demo services (Python standard library only)
│   ├── app.py                  #   shop-api / payments / alert-receiver, chosen by SERVICE_NAME
│   └── Dockerfile
├── loki/loki.yaml              # single-node Loki, filesystem storage, TSDB schema v13, retention
├── promtail/promtail.yaml      # what to tail, logfmt parsing, labels, timestamps
├── grafana/
│   ├── provisioning/datasources/loki.yaml     # Loki datasource + trace_id link
│   ├── provisioning/dashboards/provider.yaml  # loads the dashboard folder
│   ├── provisioning/alerting/                 # rules.yaml, contact-points.yaml, policies.yaml
│   └── dashboards/logging-overview.json       # the dashboard
├── scripts/
│   ├── setup.sh      # apt upgrade, Docker + Compose, Python test tools, .env
│   ├── preflight.sh  # host/network checks, pulls every image, builds the app image
│   ├── up.sh         # start, wait until healthy, print browser URLs
│   ├── smoke.sh      # 13 checks, ends with "All 13 checks passed."
│   ├── demo.sh       # scripted demo (outage -> alert, trace, brute force -> alert)
│   ├── destroy.sh    # remove containers + volumes (--all: images + .env too)
│   ├── test.sh       # runs the unit tests
│   └── lib.sh        # shared helpers (public IP via IMDSv2, .env, HTTP status checks)
├── tests/            # pytest: app logic, configs, repo sanity, scripts run with stub curl/docker
├── README.md  STEPS.md  DEMO.md  DIAGRAM_PROMPT.md
```

## Honest limitations

- **Single node, no HA.** One Loki on local disk: if the instance dies, the logs are gone. Production uses object storage (S3/GCS) and multiple replicas.
- **Promtail is end-of-life** (Grafana replaced it with Grafana Alloy in early 2026). The pinned image still pulls and works, but gets no fixes. See STEPS.md for the Alloy fallback.
- **Apps log to a shared file volume**, not via the Docker logging driver. This avoids mounting the Docker socket, but it means only apps that write to that volume are collected (Loki's and Grafana's own logs are not shipped).
- **Plain HTTP and no auth on the demo apps.** The control panel's chaos buttons and the alert inbox have no login; only the security group (your IP) protects them. Grafana has a login (random password).
- **Traffic is simulated.** Products-page latency is a generated number; checkout latency is measured for real (payments really sleeps in "slow" mode).
- **Alert tuning is demo-grade.** Short timers, and "no data" / "query error" are treated as OK so startup doesn't page anyone.
- **Not tested on a live Docker host in the build environment** — see the note at the end of STEPS.md for exactly what was and wasn't tested.
