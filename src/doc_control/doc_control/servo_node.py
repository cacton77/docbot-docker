"""
SG92R servo controller node.

Subscribes to a configurable topic (default /control/servo_angle) expecting
a std_msgs/Float32 value in degrees [min_angle, max_angle] and drives the
servo via software PWM on the specified GPIO pin.

Hardware note: physical pin 33 on the Jetson Orin Nano 40-pin header is PWM2
(hardware PWM capable). Jetson.GPIO.PWM() uses software PWM regardless of pin,
so for the smoothest output use the sysfs hardware PWM interface instead. For
an external PWM driver with I²C, a PCA9685 is a common alternative.
"""

import rclpy
from rclpy.node import Node
from std_msgs.msg import Float32

# Jetson.GPIO detects the board at import time and raises RuntimeError on
# non-Jetson hardware, so wrap the entire import.
_GPIO = None
try:
    import Jetson.GPIO as _GPIO
except Exception:
    pass


def _pulse_to_duty(pulse_us: float, freq_hz: float) -> float:
    """Convert pulse width in microseconds to PWM duty cycle percentage."""
    return pulse_us / (1_000_000.0 / freq_hz) * 100.0


class ServoNode(Node):

    def __init__(self):
        super().__init__("servo_node")

        self.declare_parameter("gpio_pin",       33)     # physical board pin — pin 33 = PWM2
        self.declare_parameter("pwm_frequency",  50.0)   # Hz — standard for servos
        self.declare_parameter("min_angle",       0.0)   # degrees
        self.declare_parameter("max_angle",     180.0)   # degrees
        self.declare_parameter("min_pulse_us",  500.0)   # pulse width at min_angle
        self.declare_parameter("max_pulse_us", 2400.0)   # pulse width at max_angle
        self.declare_parameter("command_topic", "/control/servo_angle")

        self._pin       = self.get_parameter("gpio_pin").value
        self._freq      = self.get_parameter("pwm_frequency").value
        self._min_angle = self.get_parameter("min_angle").value
        self._max_angle = self.get_parameter("max_angle").value
        self._min_pulse = self.get_parameter("min_pulse_us").value
        self._max_pulse = self.get_parameter("max_pulse_us").value
        topic           = self.get_parameter("command_topic").value

        self._pwm     = None
        self._gpio_ok = False

        if _GPIO is not None:
            try:
                _GPIO.setmode(_GPIO.BOARD)
                _GPIO.setup(self._pin, _GPIO.OUT, initial=_GPIO.LOW)
                self._pwm = _GPIO.PWM(self._pin, self._freq)
                # Start at centre position
                self._pwm.start(_pulse_to_duty(
                    (self._min_pulse + self._max_pulse) / 2.0, self._freq
                ))
                self._gpio_ok = True
                self.get_logger().info(
                    f"GPIO PWM started on board pin {self._pin} "
                    f"at {self._freq} Hz"
                )
            except Exception as e:
                self.get_logger().warn(
                    f"GPIO init failed ({e}) — running in simulation mode"
                )
        else:
            self.get_logger().warn(
                "Jetson.GPIO not available — running in simulation mode"
            )

        self.create_subscription(Float32, topic, self._cmd_cb, 10)
        self.get_logger().info(f"Subscribed to {topic}")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _angle_to_duty(self, angle: float) -> float:
        angle = max(self._min_angle, min(self._max_angle, angle))
        t = (angle - self._min_angle) / (self._max_angle - self._min_angle)
        pulse_us = self._min_pulse + t * (self._max_pulse - self._min_pulse)
        return _pulse_to_duty(pulse_us, self._freq)

    # ── Callback ──────────────────────────────────────────────────────────────

    def _cmd_cb(self, msg: Float32):
        duty = self._angle_to_duty(msg.data)
        self.get_logger().debug(
            f"angle={msg.data:.1f}°  duty={duty:.2f}%"
        )
        if self._pwm and self._gpio_ok:
            self._pwm.ChangeDutyCycle(duty)

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def destroy_node(self):
        if self._pwm:
            self._pwm.stop()
        if self._gpio_ok and _GPIO is not None:
            try:
                _GPIO.cleanup(self._pin)
            except Exception:
                pass
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = ServoNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()
