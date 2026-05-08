# OpenClaw + ROS2 Integration Plan

Status: design draft, no code committed beyond the bridge contract stub at
`src/ros2_health_bridge/bridge.py`. This document captures decisions made so
far, alternatives considered and rejected, the concrete artifact list, and
open questions outstanding.

---

## 1. Goals

Two layered goals, distinct but compatible:

**Template-level (this repo as a starting point for many robots).**
Integrate OpenClaw cleanly so any downstream project built from this template
can give an LLM access to ROS2 datastreams without re-deriving the plumbing.
The integration must be opt-in, parameterizable, and not couple the ROS2
container to a specific LLM runtime.

**Project-level (the first concrete consumer: DocBot).**
A home health-monitoring station with a thermal camera, stereo vision, rPPG
heart-rate estimation, speech-to-text, and a pose estimator. The LLM serves
two functions:

1. **Wake-word Q&A.** User says "Hey DocBot" followed by a health question.
   The agent answers, with the ability to pull live data on demand
   ("does my face look red" → capture face image; "how's my heart rate" →
   pull rPPG window).
2. **Timed health logs.** A scheduled job summarizes a structured
   per-metric health record (`/data/health/*.jsonl`, written by perception
   nodes) into human-readable Markdown digests.

Notably *not* a goal: continuous always-on LLM monitoring of streaming
sensor data. That was considered and rejected — see §4.

---

## 2. Architecture

Two containers, one shared MCP bridge between them.

```
┌─ openclaw-gateway container ────┐    ┌─ ros2 app container ───────────┐
│ ghcr.io/openclaw/openclaw       │◄──►│ ros2_health_bridge (MCP server)│
│ port 18789                      │MCP │   tools: capture_face_image,   │
│ skills + persistent memory      │    │   capture_thermal_summary,     │
│ ANTHROPIC_API_KEY from .env     │    │   get_vitals, get_activity,    │
│                                 │    │   query_health_log,            │
│                                 │    │   log_observation, escalate    │
│                                 │    │                                │
│                                 │    │ wake_word_node (porcupine /    │
│                                 │    │   openWakeWord + whisper.cpp)  │
│                                 │    │   → POSTs transcript to        │
│                                 │    │     gateway as user message    │
│                                 │    │                                │
│                                 │    │ health_logger_node             │
│                                 │    │   → /data/health/*.jsonl       │
└─────────────────────────────────┘    └────────────────────────────────┘

┌─ Timer (compose service or systemd) ──────────────────────────────────┐
│ Hourly/daily: invokes the health_log skill via gateway                │
│ Writes /data/health/summary-YYYY-MM-DD.md                             │
└───────────────────────────────────────────────────────────────────────┘
```

Both services run with `network_mode: host` (the ROS2 container already
does), so they reach each other on `localhost` without docker port mapping.

---

## 3. Why this shape

- **OpenClaw runs in its own container, not the ROS2 one.** OpenClaw expects
  to install a system daemon and persist state under `~/.openclaw/`. Forcing
  that into the ROS2 container fights its design and dies on every
  `docker compose down`. The official OpenClaw Docker setup is a dedicated
  `openclaw-gateway` container, so we use that pattern.

- **An MCP bridge, not direct topic-streaming to the LLM.** A small ROS2
  package (`ros2_health_bridge`) exposes a fixed, reviewable set of tools
  the agent can call. This gives us:
  - explicit allowlist of what the LLM can see,
  - room for perception preprocessing before data hits the model,
  - one contract to evolve (the tool list), not N adhoc subscriptions.

- **STT and wake-word stay local, not in the LLM.** OpenClaw never receives
  audio. Wake word is detected by openWakeWord/Porcupine on-device,
  transcription via whisper.cpp on-device, and only the transcript goes to
  the gateway. This is a privacy, latency, and cost decision all at once.

- **The agent only sees one image, ever.** `capture_face_image` returns a
  256×256 face crop; `capture_thermal_summary` returns text only. Raw
  stereo and full thermal frames are deliberately not on the tool surface.

- **The "timed logs" are not parsed from `rcl` output.** Perception nodes
  write structured JSONL directly to `/data/health/`. The summarization
  skill reads that, not stderr-style ROS logs. Reframing this avoided a
  brittle parsing layer.

---

## 4. Alternatives considered and rejected

| Option | Why rejected |
|---|---|
| Host-install OpenClaw via npm in `install.sh` | Initial sketch. Wrong: OpenClaw publishes an official Docker image (`ghcr.io/openclaw/openclaw`) and a `scripts/docker/setup.sh` workflow. Container path is cleaner. |
| Always-on agent loop watching every topic at full rate | Cost-prohibitive (\$10–\$100/day per robot at moderate event rates), latency-prohibitive (Claude p50 ~1–3s), and not what either of the two DocBot modes needs. |
| Two-layer perception/triage + autonomous agent | Designed for a streaming use case the user does not have. Over-engineered for wake-word Q&A + scheduled summarization. Re-evaluate if a future use case needs it. |
| Direct Anthropic SDK loop in Python, skipping OpenClaw | Considered as Option B. Rejected for this template because OpenClaw provides skills, multi-provider routing, persistent memory, and a chat-UI surface for free, and the use case is fundamentally chat-with-tools. |
| Hybrid: SDK loop for autonomous, OpenClaw for chat | Considered as Option C. Rejected for now because there is no autonomous path. Could be added later without rework — the MCP bridge stays the same. |
| Generic `subscribe(topic)` tool | Too unstructured. The LLM would be parsing JSON shapes and inventing semantics. Replaced by specific levers (`get_vitals`, `get_activity_summary`, etc.) with pre-computed summaries. |
| Parsing rcl INFO/WARN logs for the timed summary | rcl logs are debug telemetry, not health data. Replaced by a dedicated structured JSONL written directly by perception nodes. |
| Sending audio to the LLM | Privacy, cost, latency. Local STT only. |
| Sending raw thermal frames | The LLM would invent diagnoses from pixel noise. Pre-processed region-wise temperatures with deltas from baseline are the correct interface. |

---

## 5. Concrete repo additions

```
openclaw/
  openclaw.json.template       # provider config, pulls ANTHROPIC_API_KEY from env
  skills/docbot/SKILL.md       # Q&A persona, disclaimers, tool guidance
  skills/health_log/SKILL.md   # scheduled summarization template
  workspace/                   # bind-mounted to /home/node/.openclaw/workspace
config/
  health_topics.yaml           # which ROS2 topics back which bridge tools
  docbot_persona.md            # editable persona/disclaimer text
  escalation_rules.yaml        # symptoms → permitted severity / channel
src/
  ros2_health_bridge/          # MCP server + perception preprocessing
    bridge.py                  # ← contract draft already in place
  wake_word_node/              # porcupine/openWakeWord + whisper.cpp + gateway POST
  health_logger_node/          # writes /data/health/*.jsonl
data/
  health/                      # JSONL records + generated Markdown summaries
docs/
  openclaw_integration.md      # this file
```

### Changes to existing files

- **`docker-compose.yaml`** — add an `openclaw-gateway` service pinned to
  `${OPENCLAW_IMAGE:-ghcr.io/openclaw/openclaw:latest}`, bind-mount
  `./openclaw` to `/home/node/.openclaw`, pass `ANTHROPIC_API_KEY`,
  `network_mode: host`.
- **`.env`** — add `ANTHROPIC_API_KEY=`, `OPENCLAW_IMAGE=…`,
  `OPENCLAW_ENABLE=1`, plus the gateway token written by setup.sh.
- **`install.sh`** — gate an OpenClaw section behind `OPENCLAW_ENABLE`,
  invoke OpenClaw's `scripts/docker/setup.sh` once, render
  `openclaw.json` from the template substituting the key, copy
  `openclaw/skills/*` into the workspace skills directory.
- **`run.sh` / `stop.sh`** — bring both services up/down together.
- **`.gitignore`** — exclude `openclaw/workspace/` runtime state and any
  generated tokens.

### What is project-agnostic vs project-specific

| Project-agnostic (template) | Project-specific (DocBot) |
|---|---|
| `src/ros2_health_bridge/` (renamed per project) | `config/health_topics.yaml` |
| `openclaw/openclaw.json.template` | `config/escalation_rules.yaml` |
| compose service definition | `config/docbot_persona.md` |
| install.sh hook gated by `OPENCLAW_ENABLE` | the SKILL.md files |
| structured-log + skill-based summarization pattern | which topics actually exist |

That line — "edit `health_topics.yaml`, write your SKILL.md, you're done"
— is what makes the template reusable across robotics projects without
forking the integration plumbing.

---

## 6. The bridge contract

Drafted in full at `src/ros2_health_bridge/bridge.py`. Seven tools:

| Tool | Purpose | Returns |
|---|---|---|
| `capture_face_image()` | Visible-feature questions ("do I look red") | 256×256 face crop + capture metadata |
| `capture_thermal_summary()` | Temperature / fever questions | Per-region temps with deltas from baseline (text only) |
| `get_vitals(minutes_back)` | HR / HRV / SpO2 over recent window (≤180 min) | Samples + pre-computed summary |
| `get_activity_summary(hours_back)` | Movement, posture, presence (≤24 h) | Buckets + notable events |
| `query_health_log(start_iso, end_iso, metrics?)` | Anything spanning >hours | Up to 5,000 ordered records |
| `log_observation(text, …)` | Record user-reported or LLM-derived notes | The committed record |
| `escalate(reason, severity)` | Trigger real-world alert (rules-policed) | Effective severity + channel |

Design principles encoded in the contract:

- The agent never sees raw frames. Vision / thermal pre-processing happens
  in perception nodes before the bridge.
- Pre-computed summaries are first-class and the docstrings instruct the
  LLM to prefer them over re-summarizing raw arrays.
- `escalate` is policy-enforced by the bridge; the LLM cannot exceed what
  `escalation_rules.yaml` permits for the given reason.
- The audit log records every tool call and every payload sent to the
  model provider (still to be implemented).

---

## 7. Non-negotiable design points

1. **Medical-safety framing.** The system prompt for `docbot/SKILL.md`
   must state the assistant is not a doctor and not diagnostic. Hard-coded
   escalation triggers (chest pain, fainting, stroke symptoms) must
   override conversational behaviour and instruct the user to call
   emergency services.
2. **Local STT + wake word, always.** No raw audio leaves the device.
3. **Vision preprocessing in perception, not in the LLM.** No raw stereo,
   no raw thermal.
4. **Per-call audit log.** Every bridge call and its payload to the model
   provider is logged locally and viewable by the user.
5. **Provider-swap path.** OpenClaw supports local model providers; the
   config template should make it a one-line change to swap Claude for a
   local model (privacy mode).
6. **Hard interlocks live outside the LLM.** Escalation policy, command
   rate limits, allowed-action sets are enforced by the bridge or hardware,
   not by prompt instructions.
7. **No dollar-cost surprises.** Prompt-cache the system prompt and tool
   definitions aggressively. The summarization skill reuses an identical
   prompt across invocations.

---

## 8. Open questions

Carried forward from the contract review — answers will lock the next
implementation pass.

1. **Are seven tools the right cut?** Specifically: do we want a
   `get_baseline(metric)` tool so the agent can ask "what's normal for me"
   without pulling a full window?
2. **Behaviour when `face_detected=False`.** Return the latest frame
   anyway (current draft) and let the agent reposition the user, or
   refuse and return metadata only to save the image upload?
3. **Is `log_observation` agent-callable or proposal-only?** Current draft
   lets the LLM write directly. Alternative: LLM proposes, user voice
   confirmation commits. Safer but adds a turn.
4. **Severity scale.** Current draft uses info / warning / urgent /
   emergency. If the rules YAML should read more medically (e.g. NEWS2
   thresholds), set that constraint now.
5. **Thermal image for the user (not the LLM).** Useful UX but currently
   out of scope of the bridge — it's a UI concern. Confirm to keep it that
   way.
6. **arm64 image availability.** RPi target needs `linux/arm64` for
   `ghcr.io/openclaw/openclaw`. If the manifest is amd64-only, the install
   path forks: build locally on ARM, or document amd64-only support.
7. **MCP transport.** HTTP/SSE on a host port (e.g. 18790) is the current
   plan, since the bridge needs to be a long-lived rclpy node with a
   topic buffer. Confirm before pinning the port.

---

## 9. Next reviewable artifacts

In recommended order:

1. **`config/health_topics.yaml`** — the bridge's *input* contract
   (which ROS2 topics back which tools, with rate caps and message types).
2. **`openclaw/skills/docbot/SKILL.md`** — the Q&A persona + tool
   guidance + medical disclaimers.
3. **`openclaw/skills/health_log/SKILL.md`** — the scheduled summarization
   template, with a fixed Markdown output schema.
4. **`config/escalation_rules.yaml`** — the policy `escalate` enforces.
5. **`docker-compose.yaml` diff** — the openclaw-gateway service block,
   small enough to review inline.

Each is short and reviewable in isolation. None of them require any of
the others to land first; they can be done in parallel once the open
questions in §8 are settled.
