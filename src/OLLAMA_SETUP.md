# Ollama Setup for DocBot

How to bring up Qwen 2.5 on the RTX 4000 Ada host so the Jetson can reach it
over your LAN. Assumes Ubuntu / Pop!_OS / Debian-family Linux on the host. If
the host is on Windows or macOS, the Ollama installer is a one-click download
from https://ollama.com/download; the configuration steps after install are
equivalent but the file paths differ — notes inline where they matter.

The whole bring-up takes about 15 minutes plus model download time (~12 GB).

---

## 1. Verify GPU prerequisites

The RTX 4000 Ada needs a recent NVIDIA driver. Confirm CUDA is visible:

```bash
nvidia-smi
```

You should see the card listed, a driver version (≥ 535 recommended), and
"CUDA Version: 12.x" in the top-right. If `nvidia-smi` isn't found, install the
driver first — `sudo ubuntu-drivers autoinstall` on Ubuntu/Pop!_OS is the
shortest path. Ollama bundles its own CUDA runtime, so a system CUDA toolkit
install is *not* required.

---

## 2. Install Ollama

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

The installer:
- Drops the `ollama` binary in `/usr/local/bin`
- Creates an `ollama` system user
- Installs and enables a systemd unit (`/etc/systemd/system/ollama.service`)
- Starts the service immediately, bound to `127.0.0.1:11434`

Verify it's running:

```bash
systemctl status ollama
curl http://localhost:11434/api/version
```

The version endpoint should return JSON like `{"version":"0.x.x"}`.

---

## 3. Configure for LAN access

By default Ollama only listens on localhost. The Jetson needs to reach it over
the network, so override the bind address and lengthen the model keep-alive.
Use a systemd drop-in so package updates don't clobber your changes:

```bash
sudo systemctl edit ollama
```

An editor opens on an empty override file. Add:

```ini
[Service]
Environment="OLLAMA_HOST=0.0.0.0:11434"
Environment="OLLAMA_ORIGINS=*"
Environment="OLLAMA_KEEP_ALIVE=24h"
Environment="OLLAMA_FLASH_ATTENTION=1"
Environment="OLLAMA_NUM_PARALLEL=1"
```

What each does:
- `OLLAMA_HOST` — bind on all interfaces so the LAN can reach it.
- `OLLAMA_ORIGINS=*` — allow cross-origin requests; harmless on a LAN, useful
  if you later add a browser-based debug UI.
- `OLLAMA_KEEP_ALIVE=24h` — keep the model resident in VRAM rather than
  unloading after the default 5 minutes. Eliminates cold-load latency on the
  first query of the day.
- `OLLAMA_FLASH_ATTENTION=1` — enables flash attention; faster prefill on Ada.
- `OLLAMA_NUM_PARALLEL=1` — single-user setup; one in-flight request keeps VRAM
  usage predictable.

Save and reload:

```bash
sudo systemctl daemon-reload
sudo systemctl restart ollama
```

Confirm the new bind:

```bash
ss -ltnp | grep 11434
# should show 0.0.0.0:11434 (not 127.0.0.1)
```

---

## 4. Firewall: restrict to your LAN

Ollama has no authentication. Don't expose 11434 to the internet. If `ufw` is
active, allow only your local subnet — substitute your actual subnet:

```bash
sudo ufw status                            # check current state
sudo ufw allow from 192.168.1.0/24 to any port 11434 proto tcp
sudo ufw reload
```

If your network is `10.0.0.0/24` or something else, adjust accordingly. Confirm
the Jetson's IP falls inside whatever range you allow.

**Do not** run `ufw allow 11434` without the `from <subnet>` qualifier — that
would open the port to anyone who can route to your machine.

---

## 5. Pull the model

```bash
ollama pull qwen2.5:14b-instruct-q6_K
```

This downloads ~12 GB. Watch progress in the same terminal. When done:

```bash
ollama list
# qwen2.5:14b-instruct-q6_K    <hash>    12 GB    <timestamp>
```

If you want to A/B test the bigger model later:

```bash
ollama pull qwen2.5:32b-instruct-q4_K_M       # ~20 GB on disk, ~18 GB VRAM
```

You can keep multiple models pulled; Ollama only loads one at a time into VRAM
(governed by `OLLAMA_MAX_LOADED_MODELS`, default 1).

---

## 6. Smoke test on the host

A plain chat request:

```bash
curl http://localhost:11434/api/chat -s -d '{
  "model": "qwen2.5:14b-instruct-q6_K",
  "messages": [{"role": "user", "content": "In one sentence, what is a wake word?"}],
  "stream": false
}' | jq .
```

You should get JSON with a `message.content` field. First call may take 5–15 s
(cold load); subsequent calls are fast. While the call runs, check VRAM use in
another terminal:

```bash
watch -n 0.5 nvidia-smi
```

You should see ~12 GB of VRAM allocated to an `ollama` process.

---

## 7. Smoke test schema-constrained output

This is the important one — it's the path DocBot's `docbot_llm` node uses.
Save the following as `~/docbot_schema_test.json`:

```json
{
  "model": "qwen2.5:14b-instruct-q6_K",
  "messages": [
    {"role": "system", "content": "You are DocBot. Respond using the provided schema."},
    {"role": "user", "content": "I have had a mild headache for two days."}
  ],
  "stream": false,
  "format": {
    "type": "object",
    "properties": {
      "response_text":     {"type": "string"},
      "log_summary":       {"type": "string"},
      "reported_symptoms": {"type": "array", "items": {"type": "string"}},
      "observations":      {"type": "array", "items": {"type": "string"}},
      "escalation": {
        "type": "object",
        "properties": {
          "level": {"type": "string",
                    "enum": ["none","monitor","advisory","urgent","emergency"]},
          "reasoning":          {"type": "string"},
          "recommended_action": {"type": "string"}
        },
        "required": ["level","reasoning","recommended_action"]
      },
      "confidence_self_assessment": {"type": "string",
                                     "enum": ["low","medium","high"]}
    },
    "required": ["response_text","log_summary","reported_symptoms",
                 "observations","escalation","confidence_self_assessment"]
  },
  "options": {"temperature": 0.3}
}
```

Run it:

```bash
curl http://localhost:11434/api/chat -s -d @$HOME/docbot_schema_test.json \
  | jq '.message.content | fromjson'
```

You should see a parsed object that conforms exactly to the schema — including
all required fields and a sensible escalation level (likely `monitor` or
`advisory` for a 2-day mild headache).

If the inner `fromjson` fails, the model's output isn't valid JSON — that's
your signal that schema enforcement isn't working as expected on this Ollama
version. Update Ollama and retry; older releases handled `format` as a string
flag rather than a schema.

---

## 8. Test from the Jetson over the LAN

On the Jetson (or any other LAN device):

```bash
# replace 192.168.1.50 with your host's LAN IP
HOST=192.168.1.50

curl http://$HOST:11434/api/version
curl http://$HOST:11434/api/tags         # should list qwen2.5:14b-instruct-q6_K
```

If `version` works but `tags` returns an empty list, you're talking to the
service but the model didn't pull successfully — re-run step 5 on the host.

Now repeat the schema test from step 7 against the LAN address:

```bash
curl http://$HOST:11434/api/chat -s -d @$HOME/docbot_schema_test.json \
  | jq '.message.content | fromjson'
```

End-to-end. This is exactly what `docbot_llm` will do.

---

## 9. Find the host IP (for `llm.yaml`)

```bash
# on the Ollama host
ip -4 addr show | grep -E 'inet 192|inet 10|inet 172'
```

Pick the LAN-facing one and consider giving the host a static DHCP reservation
in your router so the IP doesn't drift. Then drop it into the ROS config:

```yaml
# config/llm.yaml
llm_client:
  ros__parameters:
    base_url: "http://192.168.1.50:11434/v1"
    model: "qwen2.5:14b-instruct-q6_K"
```

The trailing `/v1` is the OpenAI-compatibility path — use that base URL with
the `openai` Python SDK and existing client patterns work unchanged.

---

## 10. Performance reference (RTX 4000 Ada, Qwen 2.5 14B Q6_K)

Rough numbers to expect:

| Metric                      | Approx value          |
|-----------------------------|-----------------------|
| VRAM resident               | ~12 GB                |
| Cold load time              | 5–15 s                |
| Prefill (system + 4 turns)  | < 1 s                 |
| Generation                  | 35–55 tokens/sec      |
| 800-token response total    | ~15–22 s              |
| Idle GPU power              | ~15 W                 |
| Under generation            | ~120 W                |

If response time becomes the constraint, lower `max_tokens` to 500 in
`llm.yaml` — the `response_text` field rarely needs more than that, and the
structured fields are short.

---

## 11. Common issues

**"connection refused" from the Jetson.**
The most common cause is that `OLLAMA_HOST` isn't actually taking effect.
Re-check with `ss -ltnp | grep 11434` — if it shows `127.0.0.1:11434`, your
systemd override didn't apply. Common causes:
- Forgot to `daemon-reload` after editing.
- A stale `/etc/systemd/system/ollama.service.d/override.conf` from a previous
  attempt is overriding your override. List with `systemctl cat ollama`.

**"model requires more system memory than available".**
Usually means another model is loaded. `ollama ps` shows what's loaded;
`ollama stop <model>` unloads it. Or set `OLLAMA_MAX_LOADED_MODELS=1`.

**Wildly slow generation (< 10 tokens/sec).**
Almost always CPU fallback — the model is running on CPU because the driver
didn't initialize. Check `nvidia-smi` during generation; if VRAM use is near
zero, restart `ollama.service` and re-check the driver.

**Output isn't valid JSON despite `format` schema.**
Older Ollama versions (< 0.5) treat `format` as a plain string flag
(`"format":"json"` only) and don't honor a schema object. Upgrade to a recent
release. If you're stuck on an older version, use `format: "json"` plus the
schema in the system prompt plus the `instructor` library for validation +
retry on the client side.

**The model gets repeatedly stuck on "I'm not a doctor, please see one"
regardless of severity.**
This is RLHF safety bias, not a bug. Fix it in the system prompt with concrete
examples of each escalation level (see the few-shot anchors in
`ARCHITECTURE.md` §4.5). Lowering temperature to 0.2 also tightens adherence
to the prompt.

---

## 12. Next step

With Ollama serving the schema-constrained chat endpoint and reachable from
the Jetson, the `docbot_llm` ROS node becomes a thin wrapper: take the
incoming `QueryLLM` request, call `client.chat()` with the schema, validate the
result, populate the response. Roughly 100–150 lines of Python.
