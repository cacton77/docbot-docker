std_msgs/Header header        # same stamp as source frame

uint32          frame_width   # original image dimensions — servo uses these for angular error
uint32          frame_height

Face[]          faces         # all detections from this frame; empty = no faces detected
