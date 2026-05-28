#ifndef I2S_MIC_H
#define I2S_MIC_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// Bring up the I2S peripheral and the FreeRTOS reader task that fills the
// shared PCM FIFO. Returns false on driver init failure.
bool i2s_mic_begin(void);

// Block up to `timeout_ms` waiting for `max_bytes` of 16-bit interleaved
// stereo PCM. Returns the number of bytes actually written to `out`.
size_t i2s_mic_read_pcm16(uint8_t* out, size_t max_bytes, uint32_t timeout_ms);

// Bytes currently queued in the FIFO. Used by the USB ISR to decide whether
// to send a real packet or a silence packet on a host SOF.
size_t i2s_mic_available(void);

#ifdef __cplusplus
}
#endif

#endif  // I2S_MIC_H
