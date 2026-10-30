# Infrastructure diagram prompt (copy into an AI image model)

```
Create a clean, professional cloud infrastructure diagram in a flat, modern style (white background,
thin lines, rounded boxes, soft blue/orange/grey palette, clear readable labels, no 3D, no clutter).
Title at top: "Project 3 – Centralized Logging (Loki + Promtail + Grafana) on one EC2 instance".

Left side: a laptop icon labelled "Your browser". Three arrows go from it to the right, labelled
"http://<EC2-PUBLIC-IP>:3000 Grafana", ":8080 Shop control panel", ":5001 Alert inbox".
A fourth arrow labelled "SSH :22" goes to the same instance.

Middle: a shield-shaped boundary labelled "Security group – inbound from My IP only: 22, 3000, 8080, 5001;
outbound: all". All four arrows pass through this shield.

Inside an "AWS Cloud / Region" frame, draw one large box labelled "EC2 m7i-flex.large – Ubuntu 24.04 –
Docker Compose" containing a dashed box "Docker network" with these containers as small boxes:
- "shop-api :8080 (generates traffic)" with an arrow "HTTP + trace_id" to "payments :9000 (internal)".
- Both write arrows labelled "logfmt log lines" down into a cylinder "Shared volume /var/log/app/*.log".
- A small box outside the Docker network but inside the EC2 box: "Host /var/log/auth.log (SSH logins)".
- "Promtail (collect + parse + label)" reads from the volume cylinder and from the host auth.log box.
- Promtail arrow "push" to a database cylinder "Loki :3100 (labels index + log chunks, 72h retention)".
  Mark Loki ":3100 bound to 127.0.0.1".
- "Grafana :3000 (dashboards, Explore, alert rules)" with an arrow "LogQL queries" to Loki.
- Grafana arrow "webhook alert" to "alert-receiver :5001 (Alert inbox)".

Bottom legend: "Only ports 3000, 8080, 5001 are published to the public IP; Loki and Promtail stay on
localhost. Alerts: HighErrorRate, BruteForceLogin (per source IP), SlowCheckout (p95 from logs)."
Use small official-looking generic icons (database cylinder, shield, server, browser). Landscape 16:9.
```
