# Host setup — Debian 13 + XFCE

Turning the dedicated laptop (i5-1235U, 16 GB, 477 GB) into the Jarvis host.

Written for someone comfortable with Debian but not with running a machine headless. Every command
says what it does; nothing here is a black box. **You will not need the terminal for day-to-day use**
— that is what XFCE, Cockpit, and later Jarvis's own Files panel are for.

---

## 0 · Before you wipe anything

The install erases the disk. Copy off anything you want:

- personal files (Documents, Downloads, Desktop, Pictures)
- browser bookmarks and saved passwords — export, or sign into a sync account
- licence keys or app config you cannot re-download
- **check a second time.** There is no undo after step 2.

---

## 1 · Make the installer USB

1. Download the Debian 13 (Trixie) **netinst** image: <https://www.debian.org/distrib/netinst>
   (`debian-13.x.x-amd64-netinst.iso`, roughly 800 MB)
2. Write it to an 8 GB+ USB stick with [balenaEtcher](https://etcher.balena.io) or
   [Rufus](https://rufus.ie)
3. Boot the laptop from USB — on Lenovo, tap **F12** at power-on and pick the USB device

If it boots into Windows instead, disable **Fast Startup** in Windows first, or turn off Secure Boot
in BIOS (**F1** at power-on).

---

## 2 · Install

Choose **Graphical install** and accept the defaults, except for these three screens:

**Partitioning** — "Guided – use entire disk". Confirm you are erasing the right drive.

**User setup** — ⚠️ **leave the root password empty.** Debian then adds your user to `sudo`
automatically. If you set a root password instead, your account will *not* have admin rights and
you will have to fix it from a recovery shell.

**Software selection** — this screen decides everything:

```
[ ] Debian desktop environment     ← CHECK
      [ ] GNOME                    ← UNCHECK (heavy, ~1.8 GB idle)
      [x] Xfce                     ← CHECK (~600 MB idle)
[x] SSH server                     ← CHECK
[x] standard system utilities      ← CHECK
```

Reboot, remove the USB, log in. You should get a normal desktop with a file manager, terminal,
settings, and Firefox.

---

## 3 · First update

Open **Terminal** from the applications menu:

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y curl git python3-venv python3-pip postgresql cockpit
```

- `curl`, `git`, `python3-venv`, `pip` — needed to fetch and run Jarvis
- `postgresql` — local database
- `cockpit` — the web admin panel from step 6

---

## 4 · Stop it sleeping

**This is the step that decides whether your 24/7 server is actually 24/7.** A laptop suspends when
the lid closes; a suspended server is an offline server.

```bash
# Ignore the lid entirely
sudo sed -i 's/^#*HandleLidSwitch=.*/HandleLidSwitch=ignore/'            /etc/systemd/logind.conf
sudo sed -i 's/^#*HandleLidSwitchExternalPower=.*/HandleLidSwitchExternalPower=ignore/' /etc/systemd/logind.conf
sudo systemctl restart systemd-logind

# Disable every sleep state at the system level
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
```

Then in the XFCE menu → **Power Manager**: set *System* and *Display* blank/sleep timers to **Never**
while plugged in. (The desktop's own timers are separate from the systemd ones above; you need both.)

**Test it:** close the lid, wait a minute, and from another machine `ping` the laptop's IP. If it
answers, you are done.

---

## 5 · Protect the battery

A laptop held at 100 % charge permanently will degrade and can swell the cell. On ThinkPads, cap the
charge:

```bash
# Stop charging at 60%
echo 60 | sudo tee /sys/class/power_supply/BAT0/charge_control_end_threshold
```

Make it survive reboots:

```bash
sudo tee /etc/systemd/system/battery-cap.service >/dev/null <<'EOF'
[Unit]
Description=Cap battery charge at 60% for always-on operation
After=multi-user.target

[Service]
Type=oneshot
ExecStart=/bin/bash -c 'echo 60 > /sys/class/power_supply/BAT0/charge_control_end_threshold'

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable --now battery-cap.service
```

If that path does not exist, your model exposes it differently — install `tlp` and set
`STOP_CHARGE_THRESH_BAT0=1` instead. Skip this only if the machine will run on mains with the
battery removed.

---

## 6 · Cockpit — admin without the terminal

Already installed in step 3. Enable and find your IP:

```bash
sudo systemctl enable --now cockpit.socket
ip -4 addr show | grep inet
```

From **any other device on your network**, open `https://<that-ip>:9090` and log in with your Debian
username and password. You get a file browser, terminal, logs, service controls, updates and
resource graphs — all point-and-click.

The certificate warning on first visit is expected (it is a self-signed cert on your own LAN).

---

## 7 · Tailscale — reach Jarvis from anywhere

This is what replaces a cloud server. Free, no card, no domain.

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Follow the printed link to sign in (Google/GitHub account is fine).

Then, in the [Tailscale admin console](https://login.tailscale.com/admin):
- **DNS → enable MagicDNS**
- **DNS → enable HTTPS Certificates**
- **Access controls** → make sure `funnel` is permitted for your node

Once Jarvis is running on port 8000:

```bash
sudo tailscale funnel 8000
```

You get a permanent public URL like `https://dp.your-tailnet.ts.net` — real certificate, reachable
from cellular, installable as a PWA on your iPhone. Install the Tailscale app on your phone too, so
private-only services stay reachable without exposing them publicly.

---

## 8 · Jarvis

```bash
git clone https://github.com/dhruvparekhdp/dhruv-ai.git ~/jarvis
cd ~/jarvis
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Create `~/jarvis/.env` (see `.env.example` for every option):

```bash
JARVIS_PASSCODE=<pick something you will remember>
GROQ_API_KEY=<from console.groq.com>
GEMINI_API_KEY=<from aistudio.google.com>
DATABASE_URL=postgresql://jarvis:<password>@localhost/jarvis
```

Set up the local database:

```bash
sudo -u postgres psql -c "CREATE USER jarvis WITH PASSWORD '<password>';"
sudo -u postgres psql -c "CREATE DATABASE jarvis OWNER jarvis;"
```

Run it as a service so it starts at boot and restarts if it crashes:

```bash
sudo tee /etc/systemd/system/jarvis.service >/dev/null <<EOF
[Unit]
Description=Jarvis orchestrator
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$HOME/jarvis
EnvironmentFile=$HOME/jarvis/.env
ExecStart=$HOME/jarvis/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now jarvis
sudo systemctl status jarvis        # should say "active (running)"
```

`Restart=always` is the durability guarantee at the process level — the boot reconciliation in the
app handles the state side.

**Check it:** `curl localhost:8000/health` should report `"storage": "postgres"`,
`"storage_durable": true`, and your engines under `"available"`.

---

## 9 · Verify the whole path

1. `https://<your-tailnet-url>/health` from your phone **on cellular, Wi-Fi off** — proves Funnel works
2. Open the root URL in **iPhone Safari** → Share → **Add to Home Screen**
3. Enter your passcode; check the footer badges read `GROQ + GEMINI` and `POSTGRES`
4. Run a mission from the Missions box and watch tasks appear
5. `sudo reboot`, wait two minutes, reload — Jarvis should be back with its mission history intact

Step 5 is the real test. If it comes back by itself, the 24/7 design is working.

---

## Useful afterwards

```bash
sudo systemctl restart jarvis      # restart after changing .env
journalctl -u jarvis -f            # live logs
journalctl -u jarvis -n 100        # last 100 lines
cd ~/jarvis && git pull && sudo systemctl restart jarvis   # update
```

All of this is also clickable in Cockpit under **Services** and **Logs**.

---

## Coming in Phase 3

An **Ansible playbook** that performs steps 3–8 in one command, so a new machine — the gaming laptop,
a mini PC, a Pi — becomes a Jarvis node without following this document again. And Jarvis's own
**Files / System / Terminal panels**, so the terminal becomes optional rather than occasional.

XFCE and Cockpit stay regardless: you cannot use Jarvis to fix Jarvis, so there must always be a way
in that does not depend on it.
