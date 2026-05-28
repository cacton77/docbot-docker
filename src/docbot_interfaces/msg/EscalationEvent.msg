# Broadcast when an escalation fires. Subscribed by any UI, notification
# bridge, or external integration that wants to react.

std_msgs/Header header

string level                  # "monitor" | "advisory" | "urgent" | "emergency"
string reasoning              # model-provided rationale
string recommended_action     # model-provided action text
string audio_clip_path        # path to dumped forensic clip, "" if none
string log_entry_id           # foreign key into health log
string conversation_id
