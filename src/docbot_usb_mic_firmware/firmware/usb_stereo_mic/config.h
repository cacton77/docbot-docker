#ifndef CONFIG_H
#define CONFIG_H

// =============================================================================
// Board target: ESP32-S3-DEVKITC-1 (N8R8 — 8 MB flash, 8 MB OPI PSRAM)
// =============================================================================

// =============================================================================
// I2S — two InvenSense ICS-43434 microphones on a shared bus
// =============================================================================
// Wiring expectation: both mics share BCLK / WS / SDO. The SEL pin of one mic
// is tied to GND (left channel slot) and the other to VDD (right channel
// slot), so they alternate on the shared SDO line per WS half-cycle.
//
// ICS-43434 emits 24-bit PCM, MSB-first, one BCLK cycle after the WS edge,
// MSB-justified inside a 32-bit slot. We configure the I2S peripheral for
// 32-bit slots, stereo, std-Philips alignment, then right-shift each sample
// down to 16 bits for the USB Audio class payload.
//
// *** Pin numbers below are PLACEHOLDERS. Confirm against your wiring. ***
#define I2S_BCLK_PIN              4    // TODO: confirm pin
#define I2S_WS_PIN                5    // TODO: confirm pin (a.k.a. LRCLK)
#define I2S_DIN_PIN               6    // TODO: confirm pin (shared SDO)

#define I2S_SAMPLE_RATE_HZ        48000
#define I2S_BITS_PER_SAMPLE       32   // ICS-43434 -> 24 bits MSB-justified in 32-bit slot
#define I2S_CHANNELS              2
#define I2S_DMA_DESC_NUM          6
#define I2S_DMA_FRAMES_PER_DESC   240  // 5 ms at 48 kHz

// Bits to right-shift a 32-bit I2S word into a signed 16-bit USB sample.
// ICS-43434 puts the 24-bit sample MSB at bit 31 of the 32-bit slot, so a
// 14-bit shift keeps the high 18 bits and discards the low 14 bits of noise
// floor / unused LSBs. Increase for more headroom / less perceived volume,
// decrease for hotter signal (and earlier clipping on loud sources).
#define I2S_TO_PCM16_SHIFT        14

// =============================================================================
// USB Audio Class (UAC 1.0) — what the host sees
// =============================================================================
#define USB_VID                   0xCafe   // TinyUSB sample VID; replace if you register one
#define USB_PID                   0x4014
#define USB_MANUFACTURER_STR      "DocBot"
#define USB_PRODUCT_STR           "DocBot Stereo Mic"
#define USB_SERIAL_STR            "DOCBOT-MIC-0001"

#define USB_AUDIO_SAMPLE_RATE     48000
#define USB_AUDIO_CHANNELS        2
#define USB_AUDIO_BITS_PER_SAMPLE 16
#define USB_AUDIO_BYTES_PER_SAMPLE (USB_AUDIO_BITS_PER_SAMPLE / 8)
// Per 1 ms USB frame: sample_rate / 1000 samples * channels * bytes_per_sample.
#define USB_AUDIO_EP_SZ           (USB_AUDIO_SAMPLE_RATE / 1000 * USB_AUDIO_CHANNELS * USB_AUDIO_BYTES_PER_SAMPLE)

// FIFO between the I2S reader task and the USB ISR/callback. Sized for
// ~20 ms of stereo 16-bit @ 48 kHz to ride out USB scheduling jitter.
#define PCM_FIFO_FRAMES           (USB_AUDIO_SAMPLE_RATE / 50)  // 20 ms
#define PCM_FIFO_BYTES            (PCM_FIFO_FRAMES * USB_AUDIO_CHANNELS * USB_AUDIO_BYTES_PER_SAMPLE)

// =============================================================================
// Status LED (board RGB LED on ESP32-S3-DEVKITC-1)
// =============================================================================
// The DevKitC-1 has a WS2812 on GPIO48 (some early N8R8 revisions use GPIO38;
// adjust if your board variant differs).
#define STATUS_LED_PIN            48   // TODO: confirm pin
#define STATUS_LED_BRIGHTNESS     32

#define STATUS_COLOR_BOOTING      0xFFA500  // amber — initialising peripherals
#define STATUS_COLOR_USB_IDLE     0x0000FF  // blue  — USB enumerated, host not streaming
#define STATUS_COLOR_STREAMING    0x00FF00  // green — host actively pulling audio
#define STATUS_COLOR_FAULT        0xFF0000  // red   — I2S or USB init failure

#endif  // CONFIG_H
