# Demo — Centralized Logging (read this, then explain it)

**What it does.** Three small services (a shop, a payment service, an alert inbox) write their logs
to one place. Loki stores them, Grafana shows them on a dashboard and raises alerts when the logs
show trouble. I can break things on purpose and everyone can watch the logs, graphs and alerts react.

**Words used**
- *Log line*: one text record of something that happened, e.g. `level=ERROR service=payments status=503`.
- *Promtail*: the collector that reads log files and sends them on.
- *Loki*: the log database. *Grafana*: the web UI for dashboards, search and alerts.
- *trace_id*: a random ID that follows one customer request through every service.

**Workflow**
1. The shop runs and makes fake customer traffic; every action writes a log line.
2. Promtail reads those files (and the server's SSH login log) and sends them to Loki.
3. Grafana queries Loki every few seconds: charts, counts, latency — all calculated from log lines.
4. Alert rules run the same kind of query; if a number crosses a limit, Grafana sends an alert to the inbox page.
5. I click a `trace_id` on any error to see that one request's lines from every service.

**Examples**
- *I break the payment service* → errors jump on the dashboard → within about a minute
  **HighErrorRate** appears in the alert inbox → I click the error's trace_id and see the shop's
  error *and* the payment error that caused it, side by side.
- *I start a fake password-guessing attack* → "Failed logins by source IP" shows one IP
  (203.0.113.66) shooting up → **BruteForceLogin** fires, naming that IP.
- *I SSH into the server* → my login appears in the "EC2 host: auth.log" panel seconds later.

**One-line takeaway:** you don't log in to servers to read logs — you search them all in one
place, and the logs themselves can raise the alarm.
