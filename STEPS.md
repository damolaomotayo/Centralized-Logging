# STEPS — Project 3: Centralized Logging (Loki + Promtail + Grafana)

Copy-paste steps from an empty AWS account to a running demo. Nothing is assumed.
Laptop needs only a browser and SSH. Total time ≈ 10 minutes after the instance is up.

## 0. Prerequisites

| Item | Value |
|---|---|
| AWS account | Your "Frontiers" account. **No IAM role needed** (the project uses no AWS services). |
| Region | Any (examples assume us-east-1) |
| Instance type | `m7i-flex.large` (2 vCPU, 8 GiB). Stack needs ≈ 2 GiB at most — not tight. |
| OS / AMI | **Ubuntu Server 24.04 LTS (HVM), SSD, 64-bit (x86)** |
| Disk | 20 GiB gp3 root volume (images ≈ 1.5 GiB + logs) |
| Key pair | RSA, `.pem` format, e.g. `p3-logging-key.pem` |
| Security group | `p3-logging-sg`, inbound rules below, outbound default (All traffic) |
| Accounts / tokens | None for the stack. For option A below: a GitHub account + fine-grained token (read-only, this repo). |
| Laptop | Browser + SSH client (Linux/macOS terminal, or WSL / PowerShell on Windows) |
| Code transfer | **A.** private GitHub repo + fine-grained token, or **B.** `scp` the zip |

**Security group — inbound rules** (one rule per port, all from **My IP**; Never 0.0.0.0/0)

| Type | Port | Source | Used for |
|---|---|---|---|
| SSH | 22 | My IP | SSH from your laptop |
| Custom TCP | 3000 | My IP | Grafana (dashboards, Explore, alerting) |
| Custom TCP | 8080 | My IP | Shop control panel (chaos buttons) |
| Custom TCP | 5001 | My IP | Alert inbox (webhook receiver page) |

**Outbound:** keep the default rule (Type All traffic, Destination Anywhere-IPv4). Needed for apt and Docker Hub.
Loki (3100) and Promtail (9080) are bound to 127.0.0.1 on the instance: **no rule needed, don't add one.**

**If your IP changes** (new Wi-Fi, VPN, ISP): EC2 → Security Groups → `p3-logging-sg` → Edit inbound
rules → for each of the 4 rules pick Source "My IP" again → Save. Nothing on the instance changes.
**If the instance's public IP changes** (after stop/start): run `./scripts/up.sh` again — it re-detects
the IP, updates `.env` and Grafana's root URL, and prints the new links.

## 1. AWS console (≈ 5 min)

**1.1 Key pair** — EC2 → Network & Security → **Key Pairs** → **Create key pair**
- Name `p3-logging-key`, type **RSA**, format **.pem** → Create (the file downloads).
- On your laptop:
  ```bash
  mv ~/Downloads/p3-logging-key.pem ~/.ssh/
  chmod 400 ~/.ssh/p3-logging-key.pem
  ```
  (Windows + WSL: copy it into your WSL home first — `chmod` does not stick on `/mnt/c`.)

**1.2 Security group** — EC2 → Network & Security → **Security Groups** → **Create security group**
- Name `p3-logging-sg`, description `Project 3 logging demo`, VPC: default.
- Inbound rules: add the 4 rules from the table above (Type, Port, Source = **My IP**).
- Outbound rules: leave the default. → **Create security group**.

**1.3 Launch the instance** — EC2 → **Instances** → **Launch instances**
- Name `p3-logging`.
- AMI: **Ubuntu Server 24.04 LTS**, architecture **64-bit (x86)**.
- Instance type: **m7i-flex.large**.
- Key pair: `p3-logging-key`.
- Network settings → Edit: Auto-assign public IP **Enable**; Firewall: **Select existing security group** → `p3-logging-sg`.
- Storage: **20 GiB gp3**.
- Advanced details: leave IAM instance profile **empty**. (IMDSv2 "required" is fine — the scripts use IMDSv2.)
- **Launch instance** → wait until *Instance state = Running* and *Status checks = 2/2*. Copy the **Public IPv4 address**.

**1.4 Connect**
```bash
ssh -i ~/.ssh/p3-logging-key.pem ubuntu@<EC2-PUBLIC-IP>
```
What you should see: an `ubuntu@ip-...:~$` prompt.

## 2. Get the code onto the instance

**Option A — private GitHub repo (recommended)**
1. On GitHub create a private repo, e.g. `p3-centralized-logging`, and push the unzipped project
   (if you push from Windows, keep scripts executable: `git update-index --chmod=+x scripts/*.sh`).
2. GitHub → Settings → Developer settings → **Fine-grained tokens** → Generate: Repository access
   *Only select repositories* → this repo; Permissions → **Contents: Read-only**; expiry 7 days.
3. On the instance:
   ```bash
   cd ~ && git clone https://github.com/<your-user>/p3-centralized-logging.git
   # Username: your GitHub username   Password: paste the token (not your GitHub password)
   cd p3-centralized-logging
   ```

**Option B — copy the zip with scp**
```bash
# on your laptop
scp -i ~/.ssh/p3-logging-key.pem p3-centralized-logging.zip ubuntu@<EC2-PUBLIC-IP>:~
# on the instance
sudo apt update && sudo apt install -y unzip
unzip -q p3-centralized-logging.zip && cd p3-centralized-logging
```

## 3. Install packages (≈ 4–6 min)

Make the scripts executable (already set in the zip/repo; harmless to repeat):
```bash
chmod +x scripts/setup.sh scripts/preflight.sh scripts/up.sh scripts/smoke.sh \
         scripts/demo.sh scripts/destroy.sh scripts/test.sh scripts/lib.sh
```
Run the setup script:
```bash
./scripts/setup.sh
newgrp docker        # activates the docker group in this shell (or log out and SSH back in)
```
What `scripts/setup.sh` does (same as running these by hand):
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y ca-certificates curl jq git unzip openssl python3 python3-pytest python3-yaml
# Docker Engine + Compose plugin from Docker's apt repository
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
# then it writes .env: random GRAFANA_ADMIN_PASSWORD (openssl rand), PUBLIC_IP, pinned image names
```
Versions: Docker Engine 27+ with Compose v2.20+ (any current release works); Python 3.12 (Ubuntu 24.04 default).
If Docker's repository is unreachable, the script falls back to Ubuntu's `docker.io` + `docker-compose-v2`.

What you should see: package installs, then `Docker version …`, `Docker Compose version …`,
`Python 3.12.x`, and `Setup done. Next: ./scripts/preflight.sh`.

## 4. Preflight — pull everything before the demo (≈ 1–2 min)
```bash
./scripts/preflight.sh
```
Checks tools, Docker access, RAM ≥ 4 GiB, disk ≥ 5 GiB, Docker Hub reachability (HTTP **401 is the
correct answer** from `registry-1.docker.io/v2/`), public IP, free ports 3000/8080/5001/3100/9080,
then pulls all 4 images (3 retries each) and builds the app image.

What you should see: a list of `[ OK ]` lines ending with **`Preflight passed.`**

## 5. Unit tests (≈ 1 min, no Docker needed)
```bash
./scripts/test.sh
```
What you should see: `NN passed` (app logic, configs, repo sanity, scripts run against stub curl/docker).

## 6. Start the stack (≈ 1 min)
```bash
./scripts/up.sh
```
What you should see: `[ OK ] Loki is up` … `[ OK ] Alert receiver is up`, a `docker compose ps` table
with 6 services, then ready-to-click links:
```
Grafana dashboard : http://<EC2-PUBLIC-IP>:3000/d/logging-overview
Grafana Explore   : http://<EC2-PUBLIC-IP>:3000/explore
Shop control panel: http://<EC2-PUBLIC-IP>:8080/
Alert inbox       : http://<EC2-PUBLIC-IP>:5001/
Grafana login     : admin / <random password from .env>
```
Loki reports "not ready" for its first ~15–30 s — normal; the script waits up to 3 minutes.

## 7. Smoke test (≈ 30 s)
```bash
./scripts/smoke.sh
```
Runs on the instance against 127.0.0.1 (your security group allows only *your laptop* to reach the
public IP — even the instance itself can't). It checks containers, Loki/Promtail/Grafana health,
the Grafana→Loki datasource, dashboard, 3 alert rules, contact point, the apps, and sends a unique
marker log line end-to-end (app → file → Promtail → Loki).

What you should see: 13 `[ OK ]` lines and **`All 13 checks passed.`**

## 8. Demo

**Scripted** (pauses between steps; add `--auto` for no pauses):
```bash
./scripts/demo.sh
```
1. Baseline error counts. 2. Payments outage for 90 s → waits until **HighErrorRate** reaches the inbox
(≈ 40–90 s). 3. Prints one failing request's lines from shop-api *and* payments by `trace_id`.
4. Brute-force login for 60 s → **BruteForceLogin** for `203.0.113.66`. 5. Back to normal.

**In the browser** (do this alongside):
1. Open the **Grafana dashboard** (log in with the printed password). Log volume by service, zero errors.
2. Open the **Shop control panel** → click **Payments outage**. Within ~20 s the error panels rise.
3. Open the **Alert inbox** → **HighErrorRate** appears as *firing* (≈ 1 min), later *resolved*.
4. In the dashboard's "Errors and warnings" panel, expand a line → click the **trace_id** link →
   Explore shows that request in shop-api and payments.
5. Click **Brute-force login attack** → "Failed logins by source IP" spikes for one IP → **BruteForceLogin**.
6. Click **Slow payments** → "Checkout p95" passes 1000 ms → **SlowCheckout** after ~1 min.
7. Open a second SSH session → the "EC2 host: auth.log" panel shows the login.
8. Grafana → **Explore** → try: `{job="apps"} | logfmt | status >= 500`.

## 9. Troubleshooting

| Symptom | Fix |
|---|---|
| Browser can't open `:3000` / `:8080` / `:5001` | Check the SG has that port from **My IP**; if your IP changed, re-select "My IP" (section 0). Check `docker compose ps`. Use `http://`, not https. |
| `permission denied … docker.sock` | `newgrp docker` (or log out/in). Scripts also fall back to `sudo docker`. |
| Preflight: "Docker Hub registry not reachable" | Check outbound SG rule is All traffic; `curl -s -o /dev/null -w '%{http_code}' https://registry-1.docker.io/v2/` must print 401. |
| Image pull fails / rate limited | Re-run `./scripts/preflight.sh`. Then use a fallback image in `.env` (see *Risky dependencies*), and re-run preflight. |
| Grafana login fails | Password is in `.env` (`grep GRAFANA .env`). If `.env` was recreated after Grafana's first start: `docker compose exec grafana grafana cli admin reset-admin-password "$(grep ^GRAFANA_ADMIN_PASSWORD .env | cut -d= -f2)"`. |
| Dashboard panels say "No data" | Wait 1 minute (panels count the last 1–5 min). Check `curl -s 127.0.0.1:9080/ready` and `docker compose logs --tail=30 promtail`. |
| "EC2 host: auth.log" panel empty | rsyslog writes that file; on minimal images: `sudo apt install -y rsyslog`, then `docker compose restart promtail`. |
| Alert never fires | Grafana → Alerting → Alert rules: check state (Normal/Pending/Firing). `docker compose logs grafana \| grep -i alert`. Make sure the chaos button was clicked (status box on the control panel). |
| Loki `/ready` stays 503 | `docker compose logs --tail=50 loki`. Disk full? `df -h`. Then `./scripts/destroy.sh -y && ./scripts/up.sh`. |
| A port is already in use | `sudo ss -ltnp \| grep -E ':(3000\|8080\|5001\|3100\|9080) '` and stop that process. |
| Changed `app/app.py` but nothing changed | `up.sh` reuses the built image. Rebuild: `docker compose build && ./scripts/up.sh`. |
| Links point to an old IP | Run `./scripts/up.sh` again (re-detects the IP, updates Grafana's root URL). |
| Disk filling up | `docker system df`; `docker image prune -f`. Loki keeps 72 h only. |

## 10. Cleanup / destroy
```bash
./scripts/destroy.sh          # containers, network and volumes (all logs + Grafana state); asks first
./scripts/destroy.sh --all -y # also images and .env, no question
```
What you should see: `Stack destroyed.` Then **in the AWS console**:
1. EC2 → Instances → `p3-logging` → Instance state → **Terminate**.
2. EC2 → Security Groups → `p3-logging-sg` → **Delete** (after the instance is terminated).
3. Optional: EC2 → Key Pairs → delete `p3-logging-key`; GitHub → delete the fine-grained token.

## 11. Time and cost

| Step | Time |
|---|---|
| Console: key pair, SG, launch | ~5 min |
| `setup.sh` (apt upgrade + Docker) | 4–6 min |
| `preflight.sh` (pull + build) | 1–2 min |
| `up.sh` + `smoke.sh` | ~2 min |
| `demo.sh` | ~5 min |

Cost: m7i-flex.large ≈ US$0.10/hour on-demand (us-east-1; varies by region) + 20 GiB gp3 ≈ US$0.07/day.
A 2-hour session ≈ US$0.25. Terminate the instance when done.

## Risky dependencies and fallbacks

| Dependency | Risk | Fallback |
|---|---|---|
| `grafana/promtail:3.4.2` | Promtail is end-of-life (replaced by Grafana Alloy); image still on Docker Hub but unmaintained | Set a newer tag in `.env`, e.g. `PROMTAIL_IMAGE=grafana/promtail:3.5.<x>` (check the tag exists on Docker Hub), or migrate to `grafana/alloy` (`alloy convert --source-format=promtail promtail/promtail.yaml`) — not shipped/tested here |
| `grafana/loki`, `grafana/grafana` from Docker Hub | Pull failures / rate limits | Retry; or in `.env` use `mirror.gcr.io/grafana/loki:3.4.2` etc. (Google's Docker Hub mirror, if it has the tag cached) or a newer tag (`grafana/loki:3.5.<x>`, `grafana/grafana:12.<x>.<y>` — check it exists on Docker Hub) |
| `python:3.12-slim` | Pull failure | `.env`: `PYTHON_IMAGE=public.ecr.aws/docker/library/python:3.12-slim` (AWS ECR Public copy of the official image) |
| Docker apt repo (download.docker.com) | Unreachable | `setup.sh` falls back to Ubuntu's `docker.io` + `docker-compose-v2` automatically |
| checkip.amazonaws.com | Only used if IMDS fails | `PUBLIC_IP_OVERRIDE=<ip> ./scripts/up.sh` |

After changing `.env`, run `./scripts/preflight.sh` then `./scripts/up.sh`.
No PyPI/pip downloads are needed: the demo apps use the Python standard library only.

## What was and was not tested

- **Tested in the build environment:** `bash -n` and shellcheck on every script; `docker compose config`
  (file validity, variable interpolation, ports, memory limits); YAML/JSON parsing of all configs and
  structural checks of alert rules, datasource, dashboard queries; the Python services run for real
  (HTTP, chaos modes, logfmt output, trace_id passing, webhook parsing); every script run end-to-end
  against stub `curl`/`docker`/`sudo` that answer like the real services (incl. Docker Hub's 401,
  IMDS failures, a stopped container, Loki not ready).
- **Not tested here (no Docker daemon available):** pulling/running the Loki, Promtail and Grafana
  containers; Grafana actually loading the provisioning files; the LogQL queries against a real Loki;
  alerts actually firing. `preflight.sh` + `smoke.sh` + `demo.sh` on the instance are the checks for these —
  run them once before the live demo.
