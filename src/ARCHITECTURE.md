# DocBot Voice Interaction Architecture

A ROS 2 architecture for wake-word-triggered conversational health monitoring on a
Jetson Orin Nano, with a self-hosted LLM (Qwen 2.5 via Ollama on a LAN host with
an RTX 4000 Ada) as the reasoning core. The LLM interface is abstracted behind a
single ROS service so the reasoning backend is swappable.

---

## 0. Platform note

The `docker-compose.yaml` in `docbot-docker` builds the Jetson profile from
`nvcr.io/nvidia/isaac/ros:aarch64-ros2_humble_*` and ONNX Runtime
`dustynv/onnxruntime:1.20.2-r36.4.0`. That tag is **L4T r36.4.0 → JetPack 6.1**,
not 4.6.x. JetPack 4.6 targets Maxwell/Volta Jetsons and cannot run the Orin Nano
or current Isaac ROS at all. Everything below assumes JetPack 6.x / Ubuntu 22.04 /
ROS 2 Humble, which matches your repo.

GPU-bound stages (STT, optionally TTS) use CUDA 12 + cuDNN + TensorRT 10 on the
Orin Nano's integrated GPU. CPU-bound stages (wake word, VAD) run on ARM cores
via ONNX Runtime CPU EP — keeping them off the GPU leaves headroom for STT.

**Networked LLM host.** A separate machine on the LAN runs Ollama serving the
reasoning model. This machine has an RTX 4000 Ada (20 GB VRAM), which comfortably
hosts Qwen 2.5 14B at Q6_K or Qwen 2.5 32B at Q4_K_M. The Jetson reaches it over
the local network on the Ollama OpenAI-compatible endpoint (default port 11434).
See `OLLAMA_SETUP.md` for bring-up instructions on the host.

---

## 1. Architectural overview

```
                                  ┌────────────────────────┐
                                  │  docbot_audio          │
        mic ──ALSA/PortAudio──▶   │   AudioCaptureNode     │
                                  │   - publishes /audio   │
                                  │   - 1s pre-roll buf    │
                                  │   - 60s forensic buf   │
                                  │   - DumpBuffer service │
                                  └─────────┬──────────────┘
                                            │ /audio (AudioFrame)
              ┌─────────────────────────────┼─────────────────────────────┐
              ▼                             ▼                             ▼
    ┌──────────────────┐         ┌──────────────────┐         ┌──────────────────┐
    │ docbot_trigger   │         │ docbot_stt       │         │ (future: other   │
    │  WakeWordNode    │         │  TranscribeNode  │         │  trigger nodes,  │
    │ - openWakeWord   │         │ - faster-whisper │         │  e.g. impact /   │
    │ - publishes      │         │ - Silero VAD     │         │  fall detect)    │
    │   /trigger_event │         │ - Action server  │         └──────────────────┘
    └────────┬─────────┘         └─────────▲────────┘
             │                             │
             │ TriggerEvent                │ Transcribe.action
             ▼                             │
    ┌──────────────────────────────────────┴────────────────────────────────┐
    │  docbot_dialog / DialogOrchestratorNode                                │
    │   - State machine (IDLE→LISTENING→THINKING→RESPONDING→…)               │
    │   - Owns conversation context (rolling window of recent turns)         │
    │   - Sequences: STT action → LLM service → TTS + Log + (Escalate)       │
    └────┬─────────────────┬─────────────────────┬───────────────────┬───────┘
         │                 │                     │                   │
         │ QueryLLM.srv    │ AppendHealthLog.srv │ Escalate.srv      │ Speak.action
         ▼                 ▼                     ▼                   ▼
    ┌──────────┐   ┌────────────────┐   ┌────────────────┐   ┌──────────────┐
    │ docbot_  │   │ docbot_health  │   │ docbot_health  │   │ docbot_tts   │
    │ llm      │   │ LogNode        │   │ EscalationNode │   │ SpeakNode    │
    │ HTTP ──▶ │   │ JSONL on disk  │   │ Triggers       │   │ Piper TTS    │
    │ Ollama   │   │                │   │ DumpBuffer +   │   │              │
    │ on LAN   │   └────────────────┘   │ notify channel │   └──────────────┘
    └────┬─────┘                        └────────────────┘
         │ OpenAI-compatible REST (port 11434)
         ▼
    ╔════════════════════════════════╗
    ║  LAN host (RTX 4000 Ada, 20GB) ║
    ║  Ollama → Qwen 2.5 14B/32B     ║
    ║  Schema-constrained decoding   ║
    ╚════════════════════════════════╝
```

The orchestrator is the only node that knows the *workflow*. Every other node
implements one concern, exposes a typed interface, and is independently testable.

---

## 2. ROS 2 package layout

All under `src/` in the existing workspace.

| Package              | Type   | Purpose                                           |
|----------------------|--------|---------------------------------------------------|
| `docbot_interfaces`  | ament_cmake | All custom msgs / srvs / actions             |
| `docbot_audio`       | ament_python | Mic capture + rolling buffers                |
| `docbot_trigger`     | ament_python | Wake word detection (extensible)             |
| `docbot_stt`         | ament_python | Streaming STT with VAD endpointing           |
| `docbot_dialog`      | ament_python | Orchestrator state machine                   |
| `docbot_llm`         | ament_python | Anthropic API client                         |
| `docbot_health`      | ament_python | Log + escalation services                    |
| `docbot_tts`         | ament_python | Text-to-speech                               |
| `docbot_bringup`     | ament_cmake | Launch files, params, lifecycle config       |

Custom interfaces live in their own package so any C++ node can depend on them
without pulling Python deps, and so message changes don't trigger rebuilds of
every implementation package.

---

## 3. Custom interfaces (`docbot_interfaces`)

### 3.1 Messages

**`AudioFrame.msg`** — published by the audio capture node.
```
std_msgs/Header header        # stamp = capture time of first sample
uint32 sample_rate            # 16000
uint8  channels               # 1
uint8  sample_format          # enum: 0=PCM_S16LE, 1=PCM_F32LE
uint32 frame_index            # monotonic counter from capture start
int16[] samples               # interleaved if multi-channel
```

**`TriggerEvent.msg`** — published when any wake/trigger source fires.
```
std_msgs/Header header        # stamp = detection time
string source                 # "wake_word" | "loud_sound" | "manual" | …
string label                  # e.g. "hey_docbot", "fall_impact"
float32 confidence            # [0,1]
uint32 preroll_ms             # how much audio before stamp to include
builtin_interfaces/Duration max_listen # cap for follow-on STT
```

**`DialogState.msg`** — periodic state publish for UI / monitoring.
```
std_msgs/Header header
uint8 state                   # enum below
string current_utterance      # partial or final transcript, if any
string current_intent         # informational, set by orchestrator
# state values:
uint8 STATE_IDLE        = 0
uint8 STATE_LISTENING   = 1
uint8 STATE_THINKING    = 2
uint8 STATE_RESPONDING  = 3
uint8 STATE_ESCALATING  = 4
uint8 STATE_ERROR       = 5
```

**`EscalationEvent.msg`** — broadcast when an escalation fires.
```
std_msgs/Header header
string level                  # "monitor" | "advisory" | "urgent" | "emergency"
string reasoning              # model-provided rationale
string recommended_action     # model-provided action text
string audio_clip_path        # path to dumped forensic clip, "" if none
string log_entry_id           # foreign key into health log
```

### 3.2 Services

**`DumpAudioBuffer.srv`** — request a copy of the rolling forensic buffer.
```
builtin_interfaces/Time end_time   # latest sample to include (0 = now)
builtin_interfaces/Duration window # how far back from end_time
string tag                          # filename hint, e.g. "escalation_2026..."
---
bool success
string file_path                    # absolute path inside /data
uint32 num_samples
string message
```

**`QueryLLM.srv`** — single-turn query to the cloud LLM.
```
string user_text                    # latest user utterance
string[] context_summaries          # rolling memory of prior turns (already summarized)
string conversation_id              # UUID for the dialog session
string system_prompt_override       # optional, blank = use config default
---
bool success
string response_text                # what to speak back
string log_summary                  # one-line summary for the log
string[] reported_symptoms          # extracted symptom strings
string[] observations               # model-noted observations
string escalation_level             # "none" | "monitor" | "advisory" | "urgent" | "emergency"
string escalation_reasoning
string escalation_action
string raw_tool_input_json          # full tool-call JSON for audit
string error_message
```

**`AppendHealthLog.srv`** — write a structured entry to the health log.
```
string conversation_id
string user_text
string assistant_text
string summary
string[] reported_symptoms
string[] observations
string escalation_level
string audio_clip_path              # "" if not retained
---
bool success
string log_entry_id                 # ULID/UUID, used as foreign key
string file_path                    # actual file written to
```

**`Escalate.srv`** — instruct the escalation node to act.
```
string log_entry_id
string level
string reasoning
string recommended_action
bool   retain_audio                 # true → dump forensic buffer
---
bool success
string audio_clip_path              # populated if retain_audio
string notify_result                # e.g. "sms_sent", "noop_dry_run"
```

**`SetMicGate.srv`** — used by the orchestrator to suppress wake detection during TTS playback (prevents the system from hearing itself).
```
bool gated                          # true = trigger node ignores audio
---
bool success
```

### 3.3 Actions

**`Transcribe.action`** — start/stream/finalize a transcription session.
```
# Goal
uint32 preroll_ms                   # how much pre-trigger audio to include
builtin_interfaces/Duration max_duration
builtin_interfaces/Duration silence_timeout    # endpoint after this much silence
string language                     # "" = auto
---
# Result
bool success
string transcript                   # final, possibly punctuated
float32 confidence                  # avg/word-level, model-dependent
builtin_interfaces/Duration duration
string audio_clip_path              # of the utterance itself (for replay/debug)
string error_message
---
# Feedback
string partial_transcript           # streamed as Whisper produces segments
float32 rms_level                   # cheap "VU meter" for UI
bool   speech_active                # current VAD state
```

**`Speak.action`** — synthesize and play back a response.
```
# Goal
string text
string voice                        # voice id, "" = config default
float32 rate                        # 1.0 = normal
---
# Result
bool success
builtin_interfaces/Duration duration
---
# Feedback
float32 progress                    # 0..1
```

---

## 4. Node specifications

### 4.1 `docbot_audio / AudioCaptureNode`

**Job:** open one mic, publish frames, maintain two rolling buffers, serve dump requests.

**Publishers**
- `/audio` (`docbot_interfaces/AudioFrame`) — every frame, BEST_EFFORT, KEEP_LAST depth 32.

**Services**
- `~/dump_buffer` (`DumpAudioBuffer`) — writes WAV to `/data/audio_dumps/`.

**Parameters** (`config/audio.yaml`)
```yaml
audio_capture:
  ros__parameters:
    device: "default"          # ALSA device or PortAudio index
    sample_rate: 16000
    channels: 1
    sample_format: "S16_LE"
    frame_ms: 30               # → 480 samples/frame at 16 kHz
    preroll_seconds: 1.0       # short buffer fed to STT on trigger
    forensic_seconds: 60.0     # long buffer for escalation dumps
    dump_directory: "/data/audio_dumps"
    publish_qos: "sensor_data" # BEST_EFFORT, KEEP_LAST 32
```

**Implementation notes**
- Use `sounddevice` (PortAudio) inside the container; the Jetson Docker compose
  already mounts `/dev` and adds the right groups. Confirm `arecord -l` sees the
  device from inside the container before integrating.
- Both buffers are simple ring buffers of frames (collections.deque with maxlen).
- 16 kHz mono S16LE is the universal speech format — Silero VAD, openWakeWord,
  and Whisper all consume it natively. Don't resample downstream.

---

### 4.2 `docbot_trigger / WakeWordNode`

**Job:** continuously scan `/audio`, publish a `TriggerEvent` when the wake word fires, hold off while gated.

**Subscribers**
- `/audio` (`AudioFrame`)

**Publishers**
- `/trigger_event` (`TriggerEvent`)

**Services**
- `~/set_gate` (`SetMicGate`) — orchestrator gates during TTS playback.

**Parameters** (`config/trigger.yaml`)
```yaml
wake_word:
  ros__parameters:
    engine: "openwakeword"     # plug-and-play key
    model_path: "/models/wakeword/hey_docbot.onnx"
    threshold: 0.55            # tune empirically against false-accept rate
    refractory_ms: 1500        # ignore further triggers this long after one fires
    preroll_ms: 1000           # what to put in TriggerEvent.preroll_ms
    max_listen_seconds: 15.0
    label: "hey_docbot"
    cooldown_after_tts_ms: 400 # extra grace after orchestrator releases gate
```

**Recommended models (plug-and-play)**
- **openWakeWord** — Apache 2.0, ONNX, trains custom words on synthetic TTS data.
  Best default for a custom wake word like "Hey DocBot."
- **Picovoice Porcupine** — free for personal use, very mature, ~tens of mW, but
  custom-word training requires their console.
- **microWakeWord** — TFLM-tier, smallest footprint; overkill for Orin but useful
  if you ever push the trigger node to an ESP32 alongside the Arduino subsystem.

The node's only contract is `AudioFrame in → TriggerEvent out`, so the engine
is swappable behind a `engine:` param without touching the orchestrator.

**Future expansion**
Add a `LoudSoundNode` and a `FallDetectNode` (e.g., reading the IMU you already
have on other systems) that publish to the same `/trigger_event` topic with
different `source` values. The orchestrator branches on `source` to choose
whether to listen for speech, dump audio immediately, or skip straight to
escalation.

---

### 4.3 `docbot_stt / TranscribeNode`

**Job:** action server that, on goal, reads `/audio`, runs VAD for endpointing, streams partial transcripts as feedback, returns a final transcript.

**Subscribers**
- `/audio` (`AudioFrame`) — buffered during an active goal.

**Action servers**
- `~/transcribe` (`Transcribe`)

**Parameters** (`config/stt.yaml`)
```yaml
stt:
  ros__parameters:
    engine: "faster_whisper"
    model_size: "small.en"        # tiny / base / small / medium / large-v3
    compute_type: "int8_float16"  # good fit for Orin Nano GPU
    device: "cuda"
    language: "en"
    beam_size: 1                  # greedy → lowest latency
    vad_engine: "silero"
    vad_threshold: 0.5
    vad_min_speech_ms: 250
    vad_min_silence_ms: 800       # endpoint trigger
    max_utterance_seconds: 30.0
    emit_partials_every_ms: 500
    audio_clip_directory: "/data/utterances"
```

**Recommended models**
- **faster-whisper (CTranslate2)** — best price/perf on Jetson Orin GPU.
  `small.en` is the sweet spot: ~244M params, ~5× real-time on Orin Nano with
  int8_float16, good WER for clear close-mic speech. Bump to `medium.en` if
  accuracy matters more than latency.
- **NVIDIA Riva ASR** — first-party, TensorRT-accelerated; heavier integration
  but native to the Jetson stack. Worth it if you also want diarization or
  word-level timestamps in production.
- **whisper.cpp** — CPU fallback for the `rpi` profile in your compose file.

**Implementation notes**
- The action goal's `preroll_ms` lets the orchestrator pass through whatever the
  trigger node specified, so the user's first word isn't clipped.
- Stream Whisper segments as `partial_transcript` feedback — useful for UI and
  for an eventual "barge-in" feature.
- Endpointing: simplest reliable rule is `vad_min_silence_ms` of non-speech
  *after* at least `vad_min_speech_ms` of speech. Add a hard `max_utterance`
  guard so a stuck VAD can't hang the system.

---

### 4.4 `docbot_dialog / DialogOrchestratorNode`

**Job:** the only stateful node. Owns the conversation lifecycle.

**Subscribers**
- `/trigger_event` (`TriggerEvent`)

**Publishers**
- `/dialog_state` (`DialogState`) — 5 Hz heartbeat plus on every transition.

**Service clients**
- `stt/transcribe` (action) — listen to user.
- `llm/query` (`QueryLLM`).
- `health/append_log` (`AppendHealthLog`).
- `health/escalate` (`Escalate`).
- `tts/speak` (action).
- `wake_word/set_gate` (`SetMicGate`).
- `audio_capture/dump_buffer` (`DumpAudioBuffer`).

**Parameters** (`config/dialog.yaml`)
```yaml
dialog_orchestrator:
  ros__parameters:
    conversation_id_strategy: "ulid"      # one per wake event
    context_window_turns: 6               # last N user+assistant exchanges fed back to LLM
    summarize_after_turns: 4              # condense older turns to keep prompt small
    response_timeout_seconds: 12.0        # LLM call cap
    stt_max_listen_seconds: 15.0
    tts_gate_release_delay_ms: 400
    auto_retain_levels: ["urgent", "emergency"]   # always dump audio for these
    error_recovery_ttl_seconds: 5.0
```

**State machine**
```
IDLE
  └─on TriggerEvent → set_gate(true) → LISTENING
LISTENING
  └─send Transcribe goal
       ├─success(empty) → set_gate(false) → IDLE
       ├─success(text) → THINKING
       └─timeout/error → ERROR
THINKING
  └─call QueryLLM(text, context)
       ├─success → branch on escalation_level
       │             ├─ in auto_retain_levels: call Escalate(retain_audio=true)
       │             ├─ else if != none:       call Escalate(retain_audio=false)
       │             └─ always: call AppendHealthLog
       │           → RESPONDING
       └─error → speak fallback line → ERROR
RESPONDING
  └─send Speak goal(response_text)
       └─completion → set_gate(false) → IDLE
ESCALATING
  └─transient state during Escalate.srv; UI / notification side-effect, then RESPONDING
ERROR
  └─log → set_gate(false) → IDLE after error_recovery_ttl_seconds
```

**Context handling.** Don't naively concatenate every turn — pass the LLM
`context_summaries` (one-sentence rollups of older exchanges) plus the verbatim
recent turns. The LLM itself can be asked to produce the next summary as a
secondary field in its tool call, so summarization stays consistent.

---

### 4.5 `docbot_llm / LLMClientNode`

**Job:** wrap the LAN-hosted LLM endpoint behind a single `QueryLLM` service.
The orchestrator never imports `openai` or `requests` directly — only this node
knows what backend is in use. The same node will work against Ollama,
llama.cpp's `llama-server`, or vLLM unchanged; all three expose the same
OpenAI-compatible chat-completions API.

**Service servers**
- `~/query` (`QueryLLM`)

**Parameters** (`config/llm.yaml`)
```yaml
llm_client:
  ros__parameters:
    backend: "ollama"                      # "ollama" | "llama_cpp" | "vllm"
    base_url: "http://10.0.0.50:11434/v1"  # LAN IP of the Ollama host
    model: "qwen2.5:14b-instruct-q6_K"
    max_tokens: 800
    temperature: 0.3                       # lower than cloud default; tighter outputs
    top_p: 0.9
    timeout_seconds: 30.0                  # local generation is slower than cloud
    connect_timeout_seconds: 3.0
    retry_attempts: 2
    retry_backoff_seconds: 0.5
    warmup_on_startup: true                # avoid cold-load latency on first user turn
    keep_alive: "24h"                      # ask Ollama to keep weights resident
    schema_enforcement: "format"           # "format" | "grammar" | "prompt_only"
    system_prompt_path: "/config/prompts/system.md"
    fewshot_path: "/config/prompts/fewshot.json"
    log_raw_responses: true                # /data/llm_audit/<conversation_id>.jsonl
```

**Structured-output pattern.** Cloud APIs like Anthropic's `tool_choice` can
*force* a schema. Local models can't be forced in the same way, but Ollama,
llama.cpp, and vLLM all support **grammar-constrained decoding**, which is
stronger — it makes invalid output token-by-token impossible. The recommended
path is Ollama's `format` parameter, which accepts a JSON schema and uses
llama.cpp's GBNF under the hood:

```python
schema = {
    "type": "object",
    "properties": {
        "response_text":     {"type": "string"},
        "log_summary":       {"type": "string"},
        "reported_symptoms": {"type": "array", "items": {"type": "string"}},
        "observations":      {"type": "array", "items": {"type": "string"}},
        "escalation": {
            "type": "object",
            "properties": {
                "level":              {"type": "string",
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
}

# Ollama chat call (OpenAI-compatible SDK works too)
resp = client.chat(
    model="qwen2.5:14b-instruct-q6_K",
    messages=[{"role":"system","content":system_prompt}, *context, {"role":"user","content":user_text}],
    format=schema,                  # grammar-enforced
    options={"temperature": 0.3, "num_predict": 800},
    keep_alive="24h",
)
```

The `confidence_self_assessment` field is a small but useful trick: smaller
local models occasionally produce schema-valid but semantically empty objects.
Asking the model to self-report confidence gives the orchestrator a cheap
signal to route low-confidence answers to a softer "I'm not sure I caught
that — could you say more?" fallback rather than acting on weak data.

**Belt-and-suspenders validation.** Even with grammar-constrained decoding,
validate the parsed object with `jsonschema` on the node side. Retry once with
a slightly higher temperature on validation failure; surface persistent
failures via `QueryLLM.error_message` so the orchestrator can speak a fallback.

**System prompt structure** (`/config/prompts/system.md`). Smaller local models
need more explicit guidance than Claude — especially around the escalation
taxonomy. Structure it like this:

```
You are DocBot, an at-home wellbeing assistant. You are NOT a doctor and NOT a
diagnostic device. You converse with one user about how they are feeling,
maintain an interaction log, and decide whether each interaction needs to be
flagged for follow-up.

For every user turn, fill out the `record_interaction` schema. The escalation
levels mean exactly:

- "none"      → casual or conversational, no health content worth logging
- "monitor"   → user mentioned a mild ongoing condition; log only, no action
- "advisory"  → user should consider contacting their primary care provider in
                the next few days (e.g., persistent symptoms, medication concerns)
- "urgent"    → user should contact a medical professional today (e.g., chest
                pain at rest that resolved, fever > 39 °C, new neurological
                symptoms)
- "emergency" → instruct user to call emergency services now (e.g., chest pain
                ongoing, severe shortness of breath, signs of stroke, suicidal
                statements with plan)

Default to a LOWER level when uncertain — over-escalation desensitizes the
user. Never give specific dosages or make diagnoses. Recommend professional
contact rather than offering treatment plans. Respond conversationally in
`response_text`; reserve clinical detail for `log_summary`.
```

**Few-shot anchors** (`/config/prompts/fewshot.json`). Local models drift toward
"see a doctor" on anything health-adjacent without concrete examples. Include
2–3 example exchanges per escalation level in the few-shot file, injected as
prior `messages` in the chat completion. A minimal set:

```json
[
  {"user": "I had a great walk this morning, feeling good.",
   "level": "none",
   "summary": "User reports positive mood and physical activity."},
  {"user": "My knee's been sore for about a week from running.",
   "level": "monitor",
   "summary": "User reports mild musculoskeletal soreness, exercise-related, ongoing."},
  {"user": "I've had a headache for three days that ibuprofen isn't touching.",
   "level": "advisory",
   "summary": "Persistent headache > 72h, OTC analgesic ineffective."},
  {"user": "I'm getting numbness in my left arm and feeling lightheaded.",
   "level": "urgent",
   "summary": "Unilateral paresthesia with lightheadedness — possible cardiac or neurological etiology."},
  {"user": "I think I'm having a heart attack.",
   "level": "emergency",
   "summary": "User self-reports possible acute cardiac event."}
]
```

**Failure modes specific to local hosting** that the node must handle:
- **Connection refused / DNS failure** — host machine off, Ollama not running,
  wrong IP. Respond with `error_message` and let the orchestrator speak a
  network-failure fallback.
- **Cold load latency** — Ollama lazy-loads on first request and a cold call can
  take 5–15 s for a 14B model. Mitigate with `warmup_on_startup: true` (send a
  dummy 1-token request at node init) and `keep_alive: "24h"` so the model
  doesn't get unloaded between conversations.
- **Schema-valid garbage** — covered by the `confidence_self_assessment` field
  plus jsonschema validation plus retry.
- **Slow generation under load** — a 14B Q6 model on RTX 4000 Ada produces
  roughly 35–55 tokens/sec; an 800-token response can take ~15–22 s. Set
  `timeout_seconds` accordingly and consider streaming partials to a UI later.

**Model selection guidance.**
- **Qwen 2.5 14B Instruct, Q6_K (~12 GB)** — recommended default. Strong
  instruction following, well-supported in Ollama, leaves ~8 GB for KV cache
  and context.
- **Qwen 2.5 32B Instruct, Q4_K_M (~18 GB)** — higher reasoning quality,
  tighter on context budget. Worth A/B testing once the baseline works.
- **Newer entrants worth trying** — Qwen 3.x, Mistral Small 3.2 / Magistral,
  GLM-5.1, Gemma 3 27B. The `backend` and `model` params make all of these
  one-line swaps.

**Important practical note.** This is a research/personal project, not a
regulated medical device. The system prompt and the orchestrator's fallback
behavior both bias toward conservative recommendations and prompt professional
contact early. The escalation pathway is a safety net for the user's own
benefit, never a substitute for emergency services.

---

### 4.6 `docbot_health / LogNode`

**Job:** append-only structured log of every interaction.

**Service servers**
- `~/append_log` (`AppendHealthLog`)

**Parameters** (`config/health.yaml`)
```yaml
health_log:
  ros__parameters:
    log_directory: "/data/health_log"
    log_filename_pattern: "%Y-%m.jsonl"   # monthly rollover
    redact_phi: false                     # placeholder hook
    write_buffer_seconds: 0               # 0 = fsync each entry
```

**Storage format.** One JSON object per line (JSONL). Fields:
```json
{
  "entry_id": "01JC3...ULID",
  "conversation_id": "01JC3...",
  "timestamp_utc": "2026-05-26T18:42:11.123Z",
  "user_text": "...",
  "assistant_text": "...",
  "summary": "...",
  "reported_symptoms": [...],
  "observations": [...],
  "escalation_level": "monitor",
  "audio_clip_path": "/data/utterances/2026-05-26T18-42-09.wav",
  "llm_model": "claude-sonnet-4-6",
  "llm_audit_path": "/data/llm_audit/01JC3....json"
}
```

JSONL plays well with `jq`, `pandas.read_json(lines=True)`, and is trivially
appendable. Avoid SQLite at this stage — append-only flat files are easier to
audit, back up, and inspect.

---

### 4.7 `docbot_health / EscalationNode`

**Job:** act on escalation decisions — dump audio, notify, optionally publish to a downstream channel.

**Service servers**
- `~/escalate` (`Escalate`)

**Service clients**
- `audio_capture/dump_buffer` (`DumpAudioBuffer`)

**Publishers**
- `/escalation_event` (`EscalationEvent`) — for UI / external bridges (MQTT, ntfy, etc.)

**Parameters** (`config/escalation.yaml`)
```yaml
escalation:
  ros__parameters:
    dry_run: true                         # default ON until notification is wired
    forensic_window_seconds: 60.0
    audio_dump_directory: "/data/escalation_audio"
    notify_channel: "none"                # "none" | "ntfy" | "sms" | "webhook"
    notify_target: ""
    minimum_level_to_notify: "urgent"
```

Start `dry_run: true` so escalations only write to logs and publish to
`/escalation_event` — verify behavior on a week of real interactions before
wiring a notification path.

---

### 4.8 `docbot_tts / SpeakNode`

**Job:** turn text into audio playback. Gates wake detection while speaking.

**Action servers**
- `~/speak` (`Speak`)

**Parameters** (`config/tts.yaml`)
```yaml
tts:
  ros__parameters:
    engine: "piper"
    model_path: "/models/tts/en_US-lessac-medium.onnx"
    config_path: "/models/tts/en_US-lessac-medium.onnx.json"
    sample_rate: 22050
    playback_device: "default"
    pre_play_gate_ms: 100                 # ask wake node to gate before audio starts
    post_play_gate_ms: 400
```

**Recommended engines**
- **Piper** — fast, offline, ONNX, surprisingly natural; runs comfortably on
  Orin Nano CPU.
- **Coqui XTTS** — better quality and voice cloning, heavier; viable on Orin
  GPU.
- **NVIDIA Riva TTS** — same trade-offs as Riva ASR.

---

### 4.9 `docbot_bringup`

Holds:
- `launch/docbot.launch.py` — composes the whole graph with namespaces.
- `launch/audio_only.launch.py`, `trigger_only.launch.py`, etc. — for isolated
  testing.
- `config/*.yaml` — all parameter files referenced above.
- `config/prompts/system.md` — the LLM system prompt; check into git so prompt
  changes are reviewable.
- `config/cyclonedds.xml` — already present in your repo; keep as-is.

Use a single top-level launch with sub-launches per package; that way you can
launch any subset cleanly for bring-up.

---

## 5. Quality-of-Service summary

| Topic              | Reliability | History    | Depth | Rationale                          |
|--------------------|-------------|------------|-------|------------------------------------|
| `/audio`           | BEST_EFFORT | KEEP_LAST  | 32    | Real-time stream; losses tolerable |
| `/trigger_event`   | RELIABLE    | KEEP_LAST  | 8     | Discrete event; must not drop      |
| `/dialog_state`    | RELIABLE    | KEEP_LAST  | 1     | Latest state only                  |
| `/escalation_event`| RELIABLE    | KEEP_ALL   | 50    | Audit-relevant; never drop         |

---

## 6. Bring-up and test order

A sane build order that gives a working end-to-end demo as fast as possible:

1. **`docbot_interfaces`** — define every msg/srv/action up front. Cheap to
   change later, but freezing the contract early lets the rest develop in
   parallel.
2. **`docbot_audio`** — verify mic capture in the container, confirm 16 kHz mono
   frames arrive on `/audio`. Test `DumpBuffer` with a manual service call.
3. **`docbot_tts`** — bring up Piper, prove playback works. Simplest leaf node.
4. **`docbot_stt`** — action server with a hardcoded "listen for N seconds"
   mode first, then add VAD endpointing.
5. **`docbot_trigger`** — train/download a "Hey DocBot" openWakeWord model,
   tune `threshold` against your room. Verify gate behavior with a fake
   `SetMicGate` client.
6. **`docbot_llm`** — bring up Ollama on the LAN host first (see
   `OLLAMA_SETUP.md`), verify schema-constrained chat works end-to-end with a
   manual `curl` test, then implement the ROS node. Test the node offline with
   canned transcripts before plugging into the orchestrator.
7. **`docbot_health`** — log node first, then escalation node in `dry_run`.
8. **`docbot_dialog`** — implement the state machine last; it's just glue once
   every dependency works in isolation.
9. **Full integration** — `docbot.launch.py`, run for an evening, tune
   thresholds, then turn `dry_run` off on escalation.

---

## 7. Security and operational notes

- **LLM host network exposure.** Ollama listens on `0.0.0.0:11434` once
  configured for LAN access (see `OLLAMA_SETUP.md`). Restrict with a host
  firewall rule (`ufw allow from <subnet>`) — do not expose port 11434 to the
  internet. Ollama has no authentication of its own.
- **Audio retention.** The 60 s forensic buffer is RAM-only until an escalation
  dumps it. Make that explicit in any README — it's the difference between a
  health monitor and a surveillance device.
- **Log location.** `/data` is mounted from the host. Pick a host directory
  with appropriate disk permissions and back it up — your interaction history
  is the most valuable artifact the system produces.
- **No data leaves the LAN.** A nice property of the all-local stack: audio,
  transcripts, and LLM reasoning never cross your perimeter. The only outbound
  traffic from the system is whatever the escalation node sends (currently
  nothing in `dry_run`).
- **Failure modes that need explicit handling:** LLM host unreachable, LLM cold
  load timeout, STT returns empty string, VAD never sees silence, TTS playback
  fails. All of these should land the orchestrator in `ERROR`, speak a brief
  fallback ("Sorry, something went wrong, please try again"), and return to
  `IDLE` after the configured TTL.

---

## 8. Open decisions

A few choices worth making before writing code:

1. **Conversation persistence.** Does a wake event start a *new* conversation
   every time, or does the orchestrator coalesce events within some window
   into one conversation? Affects `context_window_turns` semantics.
2. **Barge-in.** Should the user be able to interrupt TTS playback? If yes,
   the wake node stays ungated during TTS and the orchestrator cancels the
   `Speak` goal on `/trigger_event`. Nicer UX, more complexity.
3. **Multi-user.** Single user assumed throughout. If the device serves multiple
   household members, you'll want speaker identification (Resemblyzer fits) and
   a user-id field on every log entry.
4. **Cloud LLM fallback.** The inverse of the original plan: if you ever want
   a cloud LLM available when the LAN host is down (or for higher-quality
   re-analysis of flagged escalations), the `backend` param in `llm.yaml` is
   the swap point. A second LLM client node listening on a different service
   name and routed to by the orchestrator on certain conditions is also viable.
