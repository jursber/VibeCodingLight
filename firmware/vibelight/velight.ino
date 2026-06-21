/*
 * VibeLight — ESP32-C3 三色灯控固件 V2
 *
 * 协议：SET_MULTI（cmd=1），每通道独立模式（OFF/SOLID/BLINK/BREATH）
 * GPIO: 0=绿, 1=黄, 2=红  有源低电平
 * 传输：USB 串口 115200 + BLE (NimBLE)
 */

#include <NimBLEDevice.h>
#include <esp_timer.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <string.h>

// ── 硬件引脚 ──────────────────────────────────────────────
static const int PIN_GREEN  = 0;
static const int PIN_YELLOW = 1;
static const int PIN_RED    = 2;

// ── BLE ───────────────────────────────────────────────────
#define BLE_DEVICE_NAME  "VibeLight"
#define BLE_SERVICE_UUID "e52c12b6-7ac3-4636-9c17-3d608bcea796"
#define BLE_CHAR_UUID    "e52c12b7-7ac3-4636-9c17-3d608bcea796"

// ── PWM ───────────────────────────────────────────────────
static const uint32_t LEDC_FREQ    = 5000;
static const uint8_t  LEDC_RES_BITS = 8;

// ── 协议 V2 ───────────────────────────────────────────────
static const uint8_t  MAGIC0 = 0xA5;
static const uint8_t  MAGIC1 = 0x5A;
static const uint8_t  PROTO_VER = 2;
static const uint8_t  CMD_SET_MULTI = 1;
static const uint16_t SET_MULTI_LEN = 16;

// 通道模式
static const uint8_t CH_OFF   = 0;
static const uint8_t CH_SOLID = 1;
static const uint8_t CH_BLINK = 2;
static const uint8_t CH_BREATH = 3;

static const uint16_t PERIOD_MS_MIN = 50;
static const uint16_t PERIOD_MS_MAX = 60000;
static const uint32_t PROTO_MIN_INTERVAL_MS = 4;
static const uint16_t STALE_MAGIC1_MS = 120;

#ifndef TL_PWM_TASK_PRIO
#define TL_PWM_TASK_PRIO 8
#endif
#ifndef TL_SERIAL_DRAIN_MAX
#define TL_SERIAL_DRAIN_MAX 64
#endif
#ifndef TL_BREATH_RISE_PERCEPTUAL
#define TL_BREATH_RISE_PERCEPTUAL 1
#endif

// ── 环形缓冲区 ───────────────────────────────────────────
#define RB_SIZE 256
static uint8_t  s_rb[RB_SIZE];
static uint16_t s_rbLen = 0;
static portMUX_TYPE s_rbMux = portMUX_INITIALIZER_UNLOCKED;
static unsigned long s_lastRxMs = 0;
static unsigned long s_lastProtoApplyMs = 0;

// ── 灯态（SET_MULTI）──────────────────────────────────────
static uint8_t  s_chMode[3]  = {CH_OFF, CH_OFF, CH_OFF};  // G, Y, R
static uint8_t  s_chDuty[3]  = {0, 0, 0};
static uint16_t s_blinkPeriod = 800;
static uint16_t s_breathPeriod = 3000;
static bool     s_blinkSync  = true;
static bool     s_breathSync = true;

static portMUX_TYPE s_ledMux = portMUX_INITIALIZER_UNLOCKED;

// ── CRC8 ──────────────────────────────────────────────────
static uint8_t crc8Calc(const uint8_t *data, size_t len) {
    uint8_t crc = 0;
    for (size_t i = 0; i < len; i++) {
        crc ^= data[i];
        for (int b = 0; b < 8; b++) {
            crc = (crc & 0x80) ? (uint8_t)((crc << 1) ^ 0x07) : (uint8_t)(crc << 1);
        }
    }
    return crc;
}

// ── PWM 辅助 ──────────────────────────────────────────────
static inline uint8_t dutyScaledRound(uint8_t duty, uint16_t base) {
    return (uint8_t)(((uint32_t)duty * (uint32_t)base + 127U) / 255U);
}

static void setChannelVisible(uint8_t pin, uint8_t visible) {
    if (visible > 255) visible = 255;
    ledcWrite(pin, 255 - visible);
}

static void pwmHardwareInit() {
    ledcAttach((uint8_t)PIN_GREEN,  LEDC_FREQ, LEDC_RES_BITS);
    ledcAttach((uint8_t)PIN_YELLOW, LEDC_FREQ, LEDC_RES_BITS);
    ledcAttach((uint8_t)PIN_RED,    LEDC_FREQ, LEDC_RES_BITS);
    setChannelVisible(PIN_GREEN,  0);
    setChannelVisible(PIN_YELLOW, 0);
    setChannelVisible(PIN_RED,    0);
}

// ── 呼吸包络 ──────────────────────────────────────────────
static uint16_t breathEnvelope(uint16_t lin, bool rising) {
#if TL_BREATH_RISE_PERCEPTUAL
    if (rising) {
        uint32_t t = (uint32_t)lin * (uint32_t)lin;
        return (uint16_t)((t + 127U) / 255U);
    }
#endif
    (void)rising;
    return lin;
}

// ── 环形缓冲区操作 ────────────────────────────────────────
static void rbPushUnsafe(uint8_t b) {
    if (s_rbLen >= RB_SIZE) {
        memmove(s_rb, s_rb + 1, RB_SIZE - 1);
        s_rbLen = RB_SIZE - 1;
    }
    s_rb[s_rbLen++] = b;
    s_lastRxMs = millis();
}

static void rbPush(uint8_t b) {
    portENTER_CRITICAL(&s_rbMux);
    rbPushUnsafe(b);
    portEXIT_CRITICAL(&s_rbMux);
}

// ── SET_MULTI 帧解析 ──────────────────────────────────────
static bool tryApplySetMulti(const uint8_t *f) {
    if (f[0] != MAGIC0 || f[1] != MAGIC1) return false;
    if (f[2] != PROTO_VER || f[3] != CMD_SET_MULTI) return false;
    uint8_t c = crc8Calc(&f[2], 13);
    if (c != f[15]) return false;

    unsigned long now = millis();
    if (s_lastProtoApplyMs != 0 && (now - s_lastProtoApplyMs) < PROTO_MIN_INTERVAL_MS) {
        return true;
    }
    s_lastProtoApplyMs = now;

    uint8_t  flags = f[4];
    uint16_t bp = (uint16_t)f[5] | ((uint16_t)f[6] << 8);
    uint16_t brp = (uint16_t)f[7] | ((uint16_t)f[8] << 8);
    if (bp < PERIOD_MS_MIN) bp = PERIOD_MS_MIN;
    if (bp > PERIOD_MS_MAX) bp = PERIOD_MS_MAX;
    if (brp < PERIOD_MS_MIN) brp = PERIOD_MS_MIN;
    if (brp > PERIOD_MS_MAX) brp = PERIOD_MS_MAX;

    uint8_t modes[3] = {f[9], f[10], f[11]};
    uint8_t dutys[3] = {f[12], f[13], f[14]};

    for (int i = 0; i < 3; i++) {
        if (modes[i] > CH_BREATH) modes[i] = CH_OFF;
    }

    portENTER_CRITICAL(&s_ledMux);
    s_blinkSync  = (flags & 0x01) != 0;
    s_breathSync = (flags & 0x02) != 0;
    s_blinkPeriod  = bp;
    s_breathPeriod = brp;
    for (int i = 0; i < 3; i++) {
        s_chMode[i] = modes[i];
        s_chDuty[i] = dutys[i];
    }
    portEXIT_CRITICAL(&s_ledMux);
    return true;
}

// ── 协议解析（从工作缓冲区）──────────────────────────────
static void processWorkBuffer(uint8_t *wb, uint16_t *wlen) {
    // 丢弃孤立的 MAGIC0
    if (*wlen == 1 && wb[0] == MAGIC0) {
        if (millis() - s_lastRxMs > STALE_MAGIC1_MS) {
            memmove(wb, wb + 1, --(*wlen));
        }
    }

    while (*wlen >= SET_MULTI_LEN) {
        // 寻找魔数
        size_t i = 0;
        while (i + SET_MULTI_LEN <= (size_t)*wlen &&
               !(wb[i] == MAGIC0 && wb[i + 1] == MAGIC1)) {
            i++;
        }
        if (i > 0) {
            memmove(wb, wb + i, *wlen - (uint16_t)i);
            *wlen -= (uint16_t)i;
        }
        if (*wlen < SET_MULTI_LEN) break;

        if (wb[2] != PROTO_VER || wb[3] != CMD_SET_MULTI) {
            memmove(wb, wb + 1, --(*wlen));
            continue;
        }

        if (!tryApplySetMulti(wb)) {
            memmove(wb, wb + 1, --(*wlen));
            continue;
        }
        memmove(wb, wb + SET_MULTI_LEN, *wlen - SET_MULTI_LEN);
        *wlen -= SET_MULTI_LEN;
    }

    // 丢弃非魔数开头的残留字节
    while (*wlen > 0 && wb[0] != MAGIC0) {
        memmove(wb, wb + 1, --(*wlen));
    }
}

static void processRingBuffer() {
    uint8_t wb[RB_SIZE];
    uint16_t wlen = 0;
    portENTER_CRITICAL(&s_rbMux);
    wlen = s_rbLen;
    if (wlen > RB_SIZE) wlen = RB_SIZE;
    if (wlen > 0) memcpy(wb, s_rb, wlen);
    s_rbLen = 0;
    portEXIT_CRITICAL(&s_rbMux);

    processWorkBuffer(wb, &wlen);

    if (wlen > 0) {
        portENTER_CRITICAL(&s_rbMux);
        for (uint16_t i = 0; i < wlen; i++) rbPushUnsafe(wb[i]);
        portEXIT_CRITICAL(&s_rbMux);
    }
}

// ── PWM 刷新任务 ──────────────────────────────────────────
static void pwmRefreshTask(void *param) {
    (void)param;
    const TickType_t dt = pdMS_TO_TICKS(1);
    TickType_t lastWake = xTaskGetTickCount();
    int pins[3] = {PIN_GREEN, PIN_YELLOW, PIN_RED};

    for (;;) {
        vTaskDelayUntil(&lastWake, dt);

        uint8_t modes[3], dutys[3];
        uint16_t bp, brp;
        bool bs, brs;

        portENTER_CRITICAL(&s_ledMux);
        for (int i = 0; i < 3; i++) {
            modes[i] = s_chMode[i];
            dutys[i] = s_chDuty[i];
        }
        bp  = s_blinkPeriod;
        brp = s_breathPeriod;
        bs  = s_blinkSync;
        brs = s_breathSync;
        portEXIT_CRITICAL(&s_ledMux);

        // 钳位周期范围（临界区外，避免持锁过久）
        if (bp < PERIOD_MS_MIN) bp = PERIOD_MS_MIN;
        if (bp > PERIOD_MS_MAX) bp = PERIOD_MS_MAX;
        if (brp < PERIOD_MS_MIN) brp = PERIOD_MS_MIN;
        if (brp > PERIOD_MS_MAX) brp = PERIOD_MS_MAX;

        uint64_t now_us = (uint64_t)esp_timer_get_time();

        // 计算同步闪烁相位
        bool blinkOn = true;
        {
            uint64_t span = (uint64_t)bp * 1000ULL;
            uint64_t ph = now_us % span;
            blinkOn = (ph * 2ULL < span);
        }

        // 计算同步呼吸包络
        uint16_t breathBase = 0;
        {
            uint64_t span_us = (uint64_t)brp * 1000ULL;
            if (span_us < 2000ULL) span_us = 2000ULL;
            uint64_t ph = now_us % span_us;
            uint64_t x = (ph * 510ULL) / span_us;
            bool rising = (x <= 255ULL);
            uint16_t lin = rising ? (uint16_t)x : (uint16_t)(510ULL - x);
            breathBase = breathEnvelope(lin, rising);
        }

        for (int i = 0; i < 3; i++) {
            uint8_t vis = 0;
            switch (modes[i]) {
                case CH_SOLID:
                    vis = dutys[i];
                    break;
                case CH_BLINK:
                    vis = blinkOn ? dutys[i] : 0;
                    break;
                case CH_BREATH:
                    vis = dutyScaledRound(dutys[i], breathBase);
                    break;
                default: // CH_OFF
                    vis = 0;
                    break;
            }
            setChannelVisible(pins[i], vis);
        }
    }
}

// ── BLE ───────────────────────────────────────────────────
class CmdCallbacks : public NimBLECharacteristicCallbacks {
    void onWrite(NimBLECharacteristic* pChr, NimBLEConnInfo& connInfo) override {
        (void)connInfo;
        std::string v = pChr->getValue();
        size_t n = v.length();
        if (n > 256) n = 256;
        Serial.printf("[BLE] write len=%u\n", (unsigned)n);
        portENTER_CRITICAL(&s_rbMux);
        for (size_t i = 0; i < n; i++) rbPushUnsafe((uint8_t)v[i]);
        portEXIT_CRITICAL(&s_rbMux);
    }
};

class ServerCallbacks : public NimBLEServerCallbacks {
    void onConnect(NimBLEServer* pServer, NimBLEConnInfo& connInfo) override {
        (void)pServer; (void)connInfo;
        Serial.println(F("[BLE] connected"));
    }
    void onDisconnect(NimBLEServer* pServer, NimBLEConnInfo& connInfo, int reason) override {
        (void)pServer; (void)connInfo;
        Serial.printf("[BLE] disconnected (reason=0x%02x), re-advertising\n", reason);
        delay(100);
        NimBLEDevice::startAdvertising();
    }
};

static CmdCallbacks cmdCallbacks;
static ServerCallbacks serverCallbacks;

static void initBle() {
    NimBLEDevice::init(BLE_DEVICE_NAME);
    NimBLEDevice::setMTU(128);
    NimBLEServer* pServer = NimBLEDevice::createServer();
    pServer->setCallbacks(&serverCallbacks);
    NimBLEService* pSvc = pServer->createService(BLE_SERVICE_UUID);
    NimBLECharacteristic* pChr = pSvc->createCharacteristic(
        BLE_CHAR_UUID,
        NIMBLE_PROPERTY::WRITE | NIMBLE_PROPERTY::WRITE_NR);
    pChr->setCallbacks(&cmdCallbacks);
    pSvc->start();
    NimBLEAdvertising* pAdv = NimBLEDevice::getAdvertising();
    pAdv->setConnectableMode(BLE_GAP_CONN_MODE_UND);
    pAdv->setDiscoverableMode(BLE_GAP_DISC_MODE_GEN);
    pAdv->setMinInterval(160);
    pAdv->setMaxInterval(320);
    pAdv->setPreferredParams(24, 48);
    pAdv->setName(BLE_DEVICE_NAME);
    pAdv->addServiceUUID(BLE_SERVICE_UUID);
    pAdv->enableScanResponse(true);
    NimBLEDevice::startAdvertising();
}

// ── setup / loop ──────────────────────────────────────────
void setup() {
    Serial.begin(115200);
    pwmHardwareInit();
    initBle();
    xTaskCreate(pwmRefreshTask, "tl_pwm", 6144, nullptr, TL_PWM_TASK_PRIO, nullptr);
    Serial.println(F("VibeLight firmware V2 ready (SET_MULTI)"));
}

void loop() {
    for (int n = 0; n < TL_SERIAL_DRAIN_MAX && Serial.available() > 0; n++) {
        rbPush((uint8_t)Serial.read());
    }
    processRingBuffer();
    yield();
}
