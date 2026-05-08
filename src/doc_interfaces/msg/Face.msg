std_msgs/Header   header       # stamp = source frame capture time; frame_id = camera frame

uint32            track_id     # 0 = tracking unavailable; stable across frames when tracker active
float32           confidence   # YOLO detection confidence 0–1

# Bounding box in original image (pixels)
uint32            x
uint32            y
uint32            width
uint32            height

# Sub-pixel centre — servo PID reads these directly without an extra divide
float32           center_x
float32           center_y

# Face crop scaled to crop_size × crop_size, encoding rgb8
sensor_msgs/Image crop
