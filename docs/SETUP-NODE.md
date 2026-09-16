# Adding a node to the fleet

A **node** is any machine that does work for Jarvis. The agent dials *out* to
the orchestrator and holds one connection open, so nothing listens on the node
and no port is forwarded (D-014).

Works on **Linux, Windows and macOS** — you do not need to reinstall an
operating system to join a machine to the fleet.

---

## 1 · Mint a join token

On the orchestrator, or from any browser that can reach it:

```bash
curl -X POST https://<your-host>/api/v1/nodes/enrol \
     -H "X-Jarvis-Passcode: <your passcode>" \
     -H "Content-Type: application/json" \
     -d '{"label": "bench-01"}'
```

You get a token starting with `enrol_`. It is **shown once, single-use, and
expires in 30 minutes.**

---

## 2 · Start the agent on the machine

```bash
pip install websockets

python3 nodes/pc/agent.py \
  --server wss://<your-host> \
  --enrol enrol_XXXXXXXX \
  --name bench-01 \
  --root ~/jarvis-workspace \
  --allow ls --allow cat --allow git --allow python3
```

| Flag | Meaning |
|---|---|
| `--server` | Orchestrator base URL. `ws://host:8000` on a LAN, `wss://…` over Tailscale |
| `--enrol` | The one-time token. **First start only** |
| `--root` | A directory this node may touch. Repeatable. Nothing outside is reachable |
| `--allow` | A permitted shell command. Repeatable. **Empty means allow anything** |

On success it prints `enrolled as node_…` and writes `nodes/pc/node.json`
(mode 600) holding its credentials. **Every later start needs no token:**

```bash
python3 nodes/pc/agent.py --server wss://<your-host>
```

---

## 3 · Two boundaries the node enforces itself

These are enforced on the node, not by the orchestrator — so they hold even if
the orchestrator is compromised or a model produces something malicious.

**Path jail.** Every path is resolved *first*, then checked against the roots,
so `../` cannot escape:

```
fs.read /etc/passwd                     → refused: path outside the allowed roots
fs.read ~/jarvis-workspace/../../etc/passwd → refused (resolves to /etc/passwd)
```

**Shell allowlist.** Only the first word of the command is matched:

```
shell.exec "echo hi"   → exit_code: 0
shell.exec "rm -rf /"  → refused: 'rm' is not in this node's shell allowlist
```

Leaving `--allow` empty permits any command. Only do that on a node you have
explicitly designated for untrusted code, and even then prefer a real
allowlist.

---

## 4 · Run it as a service

So it survives reboots and restarts on failure:

```bash
sudo tee /etc/systemd/system/jarvis-node.service >/dev/null <<EOF
[Unit]
Description=Jarvis node agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$HOME/jarvis
ExecStart=$HOME/jarvis/.venv/bin/python nodes/pc/agent.py \\
  --server wss://<your-host> --root $HOME/jarvis-workspace \\
  --allow ls --allow cat --allow git
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload && sudo systemctl enable --now jarvis-node
```

The agent also reconnects on its own with exponential backoff (2s → 4s → … →
60s), so a restarted orchestrator or a Wi-Fi blip recovers without help.

---

## 5 · Check it worked

```bash
curl -H "X-Jarvis-Passcode: <passcode>" https://<your-host>/api/v1/nodes
```

```json
{ "online": 1, "count": 1,
  "nodes": [{ "name": "bench-01", "platform": "linux",
              "capabilities": ["EXECUTE", "STORAGE"],
              "ram_mb": 16075, "cores": 4,
              "connected": true, "untrusted_ok": false }] }
```

Then run something on it:

```bash
curl -X POST https://<your-host>/api/v1/nodes/dispatch \
     -H "X-Jarvis-Passcode: <passcode>" -H "Content-Type: application/json" \
     -d '{"tool": "system.stats"}'
```

---

## Capabilities

A node advertises what it can do; the scheduler places work against that, never
against a hostname or IP (D-023). Detected automatically at start:

| Capability | Detected when |
|---|---|
| `EXECUTE` | always — can run commands and touch files |
| `STORAGE` | always — has disk to spare |
| `DESKTOP` | a graphical session is present (`DISPLAY`/`WAYLAND_DISPLAY`, or Windows) |
| `INFERENCE` | `ollama` is on `PATH` |
| `REPORT` | telemetry only — what an iPhone sends (D-013) |

Adding, removing or repurposing a machine needs no change on the orchestrator.

---

## Designating the sandbox

Untrusted, model-generated code only runs on a node you explicitly clear:

```bash
curl -X POST https://<your-host>/api/v1/nodes/<node_id>/trust \
     -H "X-Jarvis-Passcode: <passcode>" -H "Content-Type: application/json" \
     -d '{"untrusted_ok": true}'
```

If no node is cleared, work needing it is **refused** rather than quietly
running on a trusted machine. That refusal is the point.

---

## Removing a node

```bash
curl -X DELETE https://<your-host>/api/v1/nodes/<node_id> \
     -H "X-Jarvis-Passcode: <passcode>"
```

Its secret stops working immediately. To rejoin it needs a fresh enrolment
token.
