# Periodic state publish for UI / monitoring.
# Published at 5 Hz plus on every state transition.

std_msgs/Header header

uint8 STATE_IDLE        = 0
uint8 STATE_LISTENING   = 1
uint8 STATE_THINKING    = 2
uint8 STATE_RESPONDING  = 3
uint8 STATE_ESCALATING  = 4
uint8 STATE_ERROR       = 5
uint8 state

string current_utterance      # partial or final transcript, if any
string current_intent         # informational; set by orchestrator
string conversation_id        # active conversation, "" if none
