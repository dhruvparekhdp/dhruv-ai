# EC2 setup for Jarvis (free-tier aware)

This is a small, private, read-only Jarvis deployment. It uses SQLite on the instance and calls optional hosted model APIs. It does not run Ollama, PostgreSQL, a broker, or a local market-data database. The Jarvis repository supplies the orchestrator and adapters; it does not include a complete crypto/NSE trading system.

## 1. Confirm what "free" means for your AWS account

AWS eligibility depends on the account creation date and plan. Accounts created before July 15, 2025 used the older, time-limited Free Tier. Accounts created on or after that date can use eligible EC2 sizes for six months or until credits run out. The newer Free plan currently starts with $100 in credits and may award up to $100 more; the Free plan ends after six months or when credits run out. An account on a paid plan can accrue charges after credits/allowances are exceeded. Check **Billing and Cost Management → Free Tier / Credits** and the EC2 Free Tier page for your account and region before launching.

For 24/7 use, select only an instance type that the launch wizard explicitly labels Free Tier eligible for your account. On newer eligible accounts, `t3.small` provides 2 GiB; `t3.micro` provides 1 GiB and may be tight when Python, FastEmbed/ONNX, and system services run together. On older accounts, EC2 eligibility can be limited to a micro instance and may have expired. Do not assume "free tier" means a permanently free VM.

Keep the EBS root volume within your displayed allowance (30 GiB is a common limit in the older offer), select one instance, and avoid paid extras such as NAT Gateway, load balancer, Elastic IP, snapshots, RDS, or managed monitoring. A static public IPv4 address and data transfer can have separate pricing/allowances; review the current pricing and Free Tier panel for the selected account and region. Do not create a NAT Gateway for this single host.

## 2. Launch the VM

In AWS Console → EC2 → **Launch instance**:

1. Name: `jarvis`; AMI: Ubuntu Server 24.04 LTS, 64-bit x86.
2. Pick an eligible `t3.small` if your account explicitly lists it; otherwise pick an eligible micro and plan to keep the app workload light. Avoid Graviton/ARM for the first setup unless you intentionally choose an ARM Ubuntu AMI and validate all wheels.
3. Create/select a key pair and download the private key once. Store it outside the repository with owner-only permissions.
4. Root EBS: encrypted `gp3`, 20–30 GiB, staying within your Free Tier allowance.
5. Network: default VPC is sufficient. For the most private setup, use Tailscale and leave inbound rules empty. If using SSH instead, allow TCP 22 only from your current public IP (`x.x.x.x/32`), never `0.0.0.0/0`; remove the rule after setting up SSM/Tailscale.
6. Do not add inbound ports 8000, 5432, or 11434. Do not add an Application Load Balancer or NAT Gateway.
7. In the launch summary, confirm the instance type, storage, and account-specific free eligibility before launching.

Set a billing alert in **Billing → Budgets** (or the Free Tier usage alerts available in your account). Alerts warn you; they are not a hard spending cap. Recheck the credit/usage page after the first day and weekly thereafter. The newer Free account plan blocks charges until you upgrade, but a paid plan does not.

## 3. Connect and install the runtime

Use EC2 Instance Connect/SSH only if your security group is scoped to your IP. Better for a no-ingress setup is Systems Manager Session Manager, which needs an instance role with `AmazonSSMManagedInstanceCore` and a functioning SSM Agent; Session Manager does not require opening an inbound port. AWS may charge for other supporting resources, so do not add VPC interface endpoints for this simple public-subnet host.

After connecting:

```bash
sudo apt update
sudo apt full-upgrade -y
sudo apt install -y git python3.12 python3.12-venv python3-pip build-essential
sudo adduser --system --group --home /opt/jarvis jarvis
sudo mkdir -p /opt/jarvis
sudo chown jarvis:jarvis /opt/jarvis
sudo -u jarvis git clone https://github.com/dhruvparekhdp/dhruv-ai.git /opt/jarvis/app
cd /opt/jarvis/app
sudo -u jarvis python3.12 -m venv .venv
sudo -u jarvis .venv/bin/pip install --upgrade pip
sudo -u jarvis .venv/bin/pip install -r requirements.txt
```

The dependency install includes FastEmbed/ONNX and can take time. On a micro, create 1–2 GiB of swap if the install is killed by memory pressure. Swap uses your EBS storage and CPU, so keep it small and monitor disk/memory. Do not install Ollama on a micro instance.

## 4. Set secrets and restrict access

Create `/etc/jarvis.env` with a long unique passcode and the provider keys you actually use. Leave unused integrations blank. Do not commit this file or paste keys into a chat, issue, terminal screenshot, or web UI. Use one key per actual provider account; do not make duplicate free accounts to evade quotas. Free provider quotas and allowed models change, so check each provider's console and model page. Free-tier data handling may differ from paid API terms; send only data you are comfortable sharing with that provider.

```bash
sudo install -o root -g jarvis -m 0640 /dev/null /etc/jarvis.env
sudoedit /etc/jarvis.env
```

Example contents:

```dotenv
JARVIS_PASSCODE=replace-with-a-long-unique-random-value
JARVIS_HOST=127.0.0.1
JARVIS_PORT=8000
JARVIS_SCHEDULE=1
JARVIS_DB_PATH=/opt/jarvis/app/jarvis_system.db
GROQ_API_KEY=
GEMINI_API_KEY=
# Optional, only if configured in this app:
# SEARCH_API_KEY=
```

Confirm permissions with `sudo stat -c '%a %U:%G %n' /etc/jarvis.env` (expect `640 root:jarvis`). The app defaults to SQLite when `DATABASE_URL` is unset. Back up the SQLite DB to a private location; do not put database snapshots in a public bucket.

Create `/etc/systemd/system/jarvis.service`:

```ini
[Unit]
Description=Jarvis
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=jarvis
Group=jarvis
WorkingDirectory=/opt/jarvis/app
EnvironmentFile=/etc/jarvis.env
ExecStart=/opt/jarvis/app/.venv/bin/python -m app
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/opt/jarvis/app

[Install]
WantedBy=multi-user.target
```

Then start and inspect it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now jarvis
sudo systemctl status jarvis --no-pager
curl -fsS http://127.0.0.1:8000/health
sudo journalctl -u jarvis -n 100 --no-pager
```

The service must stay on `127.0.0.1`. Do not set `JARVIS_ALLOW_PUBLIC_BIND=1` or expose port 8000 to the internet.

## 5. Reach it without opening a public app port

### Private access (recommended)

Install Tailscale on the VM and your phone/laptop, sign both into the same personal tailnet, then use Tailscale Serve to expose the local app only to that tailnet. Keep AWS inbound rules closed. The Tailscale Personal plan currently supports up to six users and Funnel is available on all plans; confirm current plan terms if your use is commercial.

### Public access (only if you need a link anyone can open)

Tailscale Funnel can expose the app to the public internet. Keep Jarvis authentication enabled, use a strong unique passcode, and verify the passcode prompt from a private browser. Public accessibility means anyone can reach the login surface and attempt guesses, even though the app includes login throttling. Do not put trading, exchange, or brokerage credentials into Jarvis.

## 6. Configure AI and market-data integrations

This app supports Groq and Gemini API keys, plus an optional search provider key. It does not provide a general pool of interchangeable AI keys; use the configured providers and provider fallback. Monitor each provider's quota separately. The in-app cost ledger uses configured price estimates, which can differ from a provider's free allowance, so inspect its usage page as well as AWS billing.

For the crypto/NSE work, start with public/delayed data feeds whose terms permit your use. Keep the first deployment read-only: dashboards, alerts, paper trades, and logs. The existing repository's adapter contract is read-only and does not place exchange or broker orders. Do not add live keys until you have isolated them from the Jarvis process and implemented explicit order limits and approval controls.

## 7. Updates, backups, and health

```bash
sudo -u jarvis git -C /opt/jarvis/app pull --ff-only
sudo -u jarvis /opt/jarvis/app/.venv/bin/pip install -r /opt/jarvis/app/requirements.txt
sudo systemctl restart jarvis
sudo systemctl is-active jarvis
curl -fsS http://127.0.0.1:8000/health
```

Before an update, back up `jarvis_system.db` to a private encrypted location. Check `journalctl -u jarvis` after every update and verify the PWA/API from the authorized network. Delete unused AMIs, snapshots, volumes, and public addresses in the EC2 console so they do not outlive the instance and accrue charges.
