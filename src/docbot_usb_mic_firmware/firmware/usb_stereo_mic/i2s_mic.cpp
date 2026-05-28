#include "i2s_mic.h"
#include "config.h"

#include <Arduino.h>
#include <driver/i2s_std.h>
#include <freertos/FreeRTOS.h>
#include <freertos/ringbuf.h>
#include <freertos/task.h>

static i2s_chan_handle_t s_rx_handle = nullptr;
static RingbufHandle_t   s_pcm_fifo  = nullptr;
static TaskHandle_t      s_reader_task = nullptr;

// Pull one DMA chunk at a time. Each chunk is I2S_DMA_FRAMES_PER_DESC stereo
// frames @ 32-bit per channel = 240 * 2 * 4 = 1920 bytes.
static constexpr size_t I2S_CHUNK_FRAMES = I2S_DMA_FRAMES_PER_DESC;
static constexpr size_t I2S_CHUNK_BYTES  = I2S_CHUNK_FRAMES * I2S_CHANNELS * sizeof(int32_t);

static void i2s_reader_task(void*) {
    int32_t i2s_buf[I2S_CHUNK_FRAMES * I2S_CHANNELS];
    int16_t pcm_buf[I2S_CHUNK_FRAMES * I2S_CHANNELS];

    while (true) {
        size_t bytes_read = 0;
        esp_err_t err = i2s_channel_read(s_rx_handle, i2s_buf, I2S_CHUNK_BYTES,
                                         &bytes_read, portMAX_DELAY);
        if (err != ESP_OK || bytes_read == 0) {
            vTaskDelay(pdMS_TO_TICKS(1));
            continue;
        }

        const size_t n_samples = bytes_read / sizeof(int32_t);
        for (size_t i = 0; i < n_samples; ++i) {
            // I2S word is 24-bit MSB-justified in the high bits of the 32-bit
            // slot. Arithmetic right-shift preserves sign.
            int32_t s = i2s_buf[i] >> I2S_TO_PCM16_SHIFT;
            if (s >  32767) s =  32767;
            if (s < -32768) s = -32768;
            pcm_buf[i] = static_cast<int16_t>(s);
        }

        // Non-blocking send: if the host has stopped draining, drop the
        // oldest data rather than stall the I2S DMA pipeline.
        if (xRingbufferSend(s_pcm_fifo, pcm_buf, n_samples * sizeof(int16_t), 0) != pdTRUE) {
            size_t dropped_sz = 0;
            void* dropped = xRingbufferReceiveUpTo(s_pcm_fifo, &dropped_sz, 0,
                                                   n_samples * sizeof(int16_t));
            if (dropped) vRingbufferReturnItem(s_pcm_fifo, dropped);
            xRingbufferSend(s_pcm_fifo, pcm_buf, n_samples * sizeof(int16_t), 0);
        }
    }
}

bool i2s_mic_begin(void) {
    s_pcm_fifo = xRingbufferCreate(PCM_FIFO_BYTES, RINGBUF_TYPE_BYTEBUF);
    if (!s_pcm_fifo) return false;

    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    chan_cfg.dma_desc_num  = I2S_DMA_DESC_NUM;
    chan_cfg.dma_frame_num = I2S_DMA_FRAMES_PER_DESC;
    chan_cfg.auto_clear    = true;
    if (i2s_new_channel(&chan_cfg, nullptr, &s_rx_handle) != ESP_OK) return false;

    i2s_std_config_t std_cfg = {
        .clk_cfg  = I2S_STD_CLK_DEFAULT_CONFIG(I2S_SAMPLE_RATE_HZ),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT,
                                                       I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = (gpio_num_t)I2S_BCLK_PIN,
            .ws   = (gpio_num_t)I2S_WS_PIN,
            .dout = I2S_GPIO_UNUSED,
            .din  = (gpio_num_t)I2S_DIN_PIN,
            .invert_flags = { .mclk_inv = false, .bclk_inv = false, .ws_inv = false },
        },
    };
    if (i2s_channel_init_std_mode(s_rx_handle, &std_cfg) != ESP_OK) return false;
    if (i2s_channel_enable(s_rx_handle) != ESP_OK) return false;

    BaseType_t ok = xTaskCreatePinnedToCore(i2s_reader_task, "i2s_rx", 4096,
                                            nullptr, 10, &s_reader_task, 1);
    return ok == pdPASS;
}

size_t i2s_mic_read_pcm16(uint8_t* out, size_t max_bytes, uint32_t timeout_ms) {
    if (!s_pcm_fifo) return 0;
    size_t total = 0;
    TickType_t deadline = xTaskGetTickCount() + pdMS_TO_TICKS(timeout_ms);

    while (total < max_bytes) {
        TickType_t now = xTaskGetTickCount();
        TickType_t wait = (now >= deadline) ? 0 : (deadline - now);

        size_t chunk_sz = 0;
        void* chunk = xRingbufferReceiveUpTo(s_pcm_fifo, &chunk_sz, wait,
                                             max_bytes - total);
        if (!chunk || chunk_sz == 0) break;
        memcpy(out + total, chunk, chunk_sz);
        vRingbufferReturnItem(s_pcm_fifo, chunk);
        total += chunk_sz;
    }
    return total;
}

size_t i2s_mic_available(void) {
    if (!s_pcm_fifo) return 0;
    UBaseType_t free_bytes = 0;
    vRingbufferGetInfo(s_pcm_fifo, nullptr, nullptr, nullptr, nullptr, &free_bytes);
    return PCM_FIFO_BYTES - (size_t)free_bytes;
}
