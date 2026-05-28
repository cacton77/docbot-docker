// Override of TinyUSB compile-time configuration for the USB Audio Class.
//
// The arduino-esp32 core ships a tusb_config.h in its arduino_tinyusb
// component with CFG_TUD_AUDIO=0 and only CDC/MSC/HID enabled. arduino-cli
// prepends the sketch directory to the include path BEFORE the platform
// includes, so dropping this file next to the .ino redirects every
// `#include "tusb_config.h"` to this version.
//
// We disable every other device class to keep the descriptor compact and
// avoid double-claiming endpoints with the (unused) CDC interface.

#ifndef _TUSB_CONFIG_H_
#define _TUSB_CONFIG_H_

#include "sdkconfig.h"

#ifdef __cplusplus
extern "C" {
#endif

// ---------- Common ----------
#define CFG_TUSB_MCU              OPT_MCU_ESP32S3
#define CFG_TUSB_OS               OPT_OS_FREERTOS
#define CFG_TUSB_RHPORT0_MODE     (OPT_MODE_DEVICE | OPT_MODE_FULL_SPEED)

#ifndef CFG_TUSB_MEM_SECTION
#define CFG_TUSB_MEM_SECTION
#endif
#ifndef CFG_TUSB_MEM_ALIGN
#define CFG_TUSB_MEM_ALIGN        __attribute__ ((aligned(4)))
#endif

#define CFG_TUD_ENDPOINT0_SIZE    64

// ---------- Disable everything we don't use ----------
#define CFG_TUD_CDC               0
#define CFG_TUD_MSC               0
#define CFG_TUD_HID               0
#define CFG_TUD_MIDI              0
#define CFG_TUD_VENDOR            0
#define CFG_TUD_DFU               0
#define CFG_TUD_DFU_RUNTIME       0
#define CFG_TUD_BTH               0
#define CFG_TUD_ECM_RNDIS         0
#define CFG_TUD_NCM               0

// ---------- Audio Class ----------
#define CFG_TUD_AUDIO                                 1

// One Audio Function (one streaming interface + control interface)
#define CFG_TUD_AUDIO_FUNC_1_N_AS_INT                 1
// We don't expose an interrupt status endpoint.
#define CFG_TUD_AUDIO_INT_CTR_EPSIZE_IN               0

// AC + AS descriptors emitted by usb_descriptors.c. The numbers MUST match
// the lengths actually produced there (see TUD_AUDIO_MIC_TWO_CH_DESC_LEN +
// trailing class-specific AS bytes). The macros TUD_AUDIO_DESC_*_LEN in
// audio_device.h give the breakdown; we just sum them once at runtime in
// the static_assert inside usb_descriptors.c.
#define CFG_TUD_AUDIO_FUNC_1_DESC_LEN                 (TUD_AUDIO_DESC_IAD_LEN \
                                                        + TUD_AUDIO_DESC_STD_AC_LEN \
                                                        + TUD_AUDIO_DESC_CS_AC_LEN \
                                                        + TUD_AUDIO_DESC_INPUT_TERM_LEN \
                                                        + TUD_AUDIO_DESC_OUTPUT_TERM_LEN \
                                                        + TUD_AUDIO_DESC_FEATURE_UNIT_TWO_CHANNEL_LEN \
                                                        + TUD_AUDIO_DESC_STD_AS_INT_LEN \
                                                        + TUD_AUDIO_DESC_STD_AS_INT_LEN \
                                                        + TUD_AUDIO_DESC_CS_AS_INT_LEN \
                                                        + TUD_AUDIO_DESC_TYPE_I_FORMAT_LEN \
                                                        + TUD_AUDIO_DESC_STD_AS_ISO_EP_LEN \
                                                        + TUD_AUDIO_DESC_CS_AS_ISO_EP_LEN)

#define CFG_TUD_AUDIO_FUNC_1_N_CHANNELS_TX            2
#define CFG_TUD_AUDIO_FUNC_1_N_BYTES_PER_SAMPLE_TX    2     // 16-bit PCM
#define CFG_TUD_AUDIO_FUNC_1_FORMAT_1_N_BYTES_PER_SAMPLE_TX 2
#define CFG_TUD_AUDIO_FUNC_1_FORMAT_1_RESOLUTION_TX   16

// 1 ms full-speed frames -> sample_rate/1000 * channels * bytes_per_sample.
// 48000 Hz stereo 16-bit = 192 bytes. Round up to a multiple of 4 for safety.
#define CFG_TUD_AUDIO_FUNC_1_EP_IN_SZ_MAX             196
#define CFG_TUD_AUDIO_FUNC_1_EP_IN_SW_BUF_SZ          (CFG_TUD_AUDIO_FUNC_1_EP_IN_SZ_MAX * 2)

// One Type-I PCM format, two-channel feature unit (Mute, Volume).
#define CFG_TUD_AUDIO_FUNC_1_N_FORMATS                1
#define CFG_TUD_AUDIO_FUNC_1_CTRL_BUF_SZ              64

#ifdef __cplusplus
}
#endif

#endif  // _TUSB_CONFIG_H_
