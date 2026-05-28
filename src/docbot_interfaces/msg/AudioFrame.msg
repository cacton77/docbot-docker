# A single frame of raw audio captured from a microphone.
# Stamp is the capture time of the FIRST sample in this frame.

std_msgs/Header header

uint32 sample_rate            # Hz, typically 16000
uint8  channels               # typically 1 (mono)

uint8 SAMPLE_FORMAT_PCM_S16LE = 0
uint8 SAMPLE_FORMAT_PCM_F32LE = 1
uint8  sample_format

uint32 frame_index            # monotonic counter from capture start
int16[] samples               # interleaved if channels > 1
