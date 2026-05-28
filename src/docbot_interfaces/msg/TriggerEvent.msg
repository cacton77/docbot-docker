# Published when any wake/trigger source fires. Multiple producer nodes
# (wake word, loud-sound detector, fall detector, manual button) all publish
# to the same topic with different `source` values; the orchestrator
# branches on `source` to decide what to do.

std_msgs/Header header

string source                 # "wake_word" | "loud_sound" | "manual" | ...
string label                  # e.g. "hey_docbot", "fall_impact"
float32 confidence            # [0,1]; producer-defined

uint32 preroll_ms             # how much audio before stamp to include in STT

builtin_interfaces/Duration max_listen   # cap for follow-on STT
