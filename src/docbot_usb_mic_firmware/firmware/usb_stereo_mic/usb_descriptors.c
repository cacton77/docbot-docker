// USB Audio Class (UAC 1.0) descriptors for a stereo, 16-bit, 48 kHz mic.
// Compiled as C so the strict-aliasing rules in tusb.h's descriptor macros
// behave the same as in upstream TinyUSB examples.

#include "tusb.h"
#include "config.h"

// ----------------------------------------------------------------------------
// Device descriptor — Class is set to "Use IAD" so an Interface Association
// Descriptor inside the configuration can group the Audio Control + Audio
// Streaming interfaces into one logical function.
// ----------------------------------------------------------------------------
tusb_desc_device_t const desc_device = {
    .bLength            = sizeof(tusb_desc_device_t),
    .bDescriptorType    = TUSB_DESC_DEVICE,
    .bcdUSB             = 0x0200,
    .bDeviceClass       = TUSB_CLASS_MISC,
    .bDeviceSubClass    = MISC_SUBCLASS_COMMON,
    .bDeviceProtocol    = MISC_PROTOCOL_IAD,
    .bMaxPacketSize0    = CFG_TUD_ENDPOINT0_SIZE,
    .idVendor           = USB_VID,
    .idProduct          = USB_PID,
    .bcdDevice          = 0x0100,
    .iManufacturer      = 0x01,
    .iProduct           = 0x02,
    .iSerialNumber      = 0x03,
    .bNumConfigurations = 0x01,
};

uint8_t const* tud_descriptor_device_cb(void) {
    return (uint8_t const*) &desc_device;
}

// ----------------------------------------------------------------------------
// Configuration descriptor — one Audio Function containing:
//   • Audio Control interface (iAC=0)
//     • Header + Input Terminal (Mic) + Feature Unit + Output Terminal (USB)
//   • Audio Streaming interface (iAS=1)
//     • Alt 0: zero-bandwidth
//     • Alt 1: 16-bit / 48 kHz / stereo on EP1 IN (isochronous, 1 ms)
// ----------------------------------------------------------------------------
enum {
    ITF_NUM_AUDIO_CONTROL = 0,
    ITF_NUM_AUDIO_STREAMING,
    ITF_NUM_TOTAL
};

#define EPNUM_AUDIO_IN        0x81

uint8_t const desc_configuration[] = {
    // Config: 1 config, total length filled in by macro at the bottom
    TUD_CONFIG_DESCRIPTOR(1, ITF_NUM_TOTAL,
                          0, /* string idx */
                          CFG_TUD_AUDIO_FUNC_1_DESC_LEN + TUD_CONFIG_DESC_LEN,
                          0x00, /* attribute: bus-powered */
                          100   /* mA */),

    // Audio Function: stereo PCM mic. The TUD_AUDIO_MIC_TWO_CH_DESCRIPTOR
    // macro emits the IAD + AC + AS descriptors in one shot.
    TUD_AUDIO_MIC_TWO_CH_DESCRIPTOR(
        /*_itfnum*/        ITF_NUM_AUDIO_CONTROL,
        /*_stridx*/        0,
        /*_nBytesPerSample*/ CFG_TUD_AUDIO_FUNC_1_N_BYTES_PER_SAMPLE_TX,
        /*_nBitsUsedPerSample*/ CFG_TUD_AUDIO_FUNC_1_FORMAT_1_RESOLUTION_TX,
        /*_epin*/          EPNUM_AUDIO_IN,
        /*_epsize*/        CFG_TUD_AUDIO_FUNC_1_EP_IN_SZ_MAX
    ),
};

uint8_t const* tud_descriptor_configuration_cb(uint8_t index) {
    (void) index;
    return desc_configuration;
}

// ----------------------------------------------------------------------------
// String descriptors
// ----------------------------------------------------------------------------
static char const* const string_desc_arr[] = {
    (const char[]) { 0x09, 0x04 }, // 0: supported language = English (0x0409)
    USB_MANUFACTURER_STR,           // 1
    USB_PRODUCT_STR,                // 2
    USB_SERIAL_STR,                 // 3
    "DocBot Stereo Mic Streaming",  // 4: AS interface name
};

static uint16_t _desc_str[33];

uint16_t const* tud_descriptor_string_cb(uint8_t index, uint16_t langid) {
    (void) langid;

    uint8_t chr_count = 0;
    if (index == 0) {
        memcpy(&_desc_str[1], string_desc_arr[0], 2);
        chr_count = 1;
    } else if (index < (sizeof(string_desc_arr) / sizeof(string_desc_arr[0]))) {
        const char* str = string_desc_arr[index];
        chr_count = (uint8_t) strlen(str);
        if (chr_count > 31) chr_count = 31;
        for (uint8_t i = 0; i < chr_count; ++i) {
            _desc_str[1 + i] = str[i];
        }
    } else {
        return NULL;
    }

    _desc_str[0] = (uint16_t) ((TUSB_DESC_STRING << 8) | (2 * chr_count + 2));
    return _desc_str;
}
