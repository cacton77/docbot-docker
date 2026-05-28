#ifndef USB_AUDIO_H
#define USB_AUDIO_H

#ifdef __cplusplus
extern "C" {
#endif

// Bring up TinyUSB with our custom UAC 1.0 stereo-microphone descriptors and
// start the FreeRTOS task that calls tud_task(). Must be called from setup()
// AFTER i2s_mic_begin() so the audio FIFO exists when the host attaches.
bool usb_audio_begin(void);

// True when an Audio Streaming interface alt-setting != 0 is active, i.e. the
// host has opened the microphone for capture. Driven by tud_audio_set_itf_cb.
bool usb_audio_is_streaming(void);

#ifdef __cplusplus
}
#endif

#endif  // USB_AUDIO_H
