/*
 * VibeLight — ESP32-C3 V2 serial-only
 * SET_MULTI (cmd=1), GPIO 0=绿 1=黄 2=红, active-low
 */

#include <esp_timer.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <string.h>

/* ── 硬件 ─────────────────────────────────── */
#define PIN_G  0
#define PIN_Y  1
#define PIN_R  2
#define LEDC_FREQ  5000
#define LEDC_BITS  8

/* ── 协议 ─────────────────────────────────── */
#define MAGIC0     0xA5
#define MAGIC1     0x5A
#define PROTO_VER  2
#define CMD_MULTI  1
#define FRAME_LEN  16

#define CH_OFF    0
#define CH_SOLID  1
#define CH_BLINK  2
#define CH_BREATH 3

#define PERIOD_MIN   50
#define PERIOD_MAX   60000
#define MIN_INTERVAL 4
#define STALE_MS     120

/* 通信看门狗：超过此时间未收到有效帧，自动全灭 */
#define WATCHDOG_MS 15000

#define RB_SIZE 256
#define DRAIN_MAX 64
#define TASK_PRIO 8
#define TASK_STACK 6144

/* ── 状态 ─────────────────────────────────── */
static uint8_t  rb[RB_SIZE];
static uint16_t rbLen = 0;
static portMUX_TYPE rbMux = portMUX_INITIALIZER_UNLOCKED;
static unsigned long lastRxMs = 0;
static unsigned long lastApplyMs = 0;

static uint8_t  chMode[3]  = {CH_OFF, CH_OFF, CH_OFF};
static uint8_t  chDuty[3]  = {0, 0, 0};
static uint16_t blinkPer   = 800;
static uint16_t breathPer  = 3000;
static portMUX_TYPE ledMux = portMUX_INITIALIZER_UNLOCKED;
static unsigned long lastValidFrameMs = 0;  /* 通信看门狗 */

/* ── CRC8 ────────────────────────────────── */
static uint8_t crc8(const uint8_t *d, size_t n) {
    uint8_t c = 0;
    for (size_t i = 0; i < n; i++) {
        c ^= d[i];
        for (int b = 0; b < 8; b++)
            c = (c & 0x80) ? (uint8_t)((c << 1) ^ 7) : (uint8_t)(c << 1);
    }
    return c;
}

/* ── PWM ─────────────────────────────────── */
static void setVis(uint8_t pin, uint8_t v) {
    ledcWrite(pin, 255 - v);  /* active-low */
}

static void pwmInit(void) {
    ledcAttach(PIN_G, LEDC_FREQ, LEDC_BITS);
    ledcAttach(PIN_Y, LEDC_FREQ, LEDC_BITS);
    ledcAttach(PIN_R, LEDC_FREQ, LEDC_BITS);
    setVis(PIN_G, 0);
    setVis(PIN_Y, 0);
    setVis(PIN_R, 0);
}

/* ── 呼吸曲线 ────────────────────────────── */
static uint16_t breathCurve(uint16_t lin, bool rise) {
    if (rise) { uint32_t t = (uint32_t)lin * lin; return (uint16_t)((t + 127) / 255); }
    return lin;
}

/* ── 环形缓冲区 ─────────────────────────── */
static void rbPush(uint8_t b) {
    portENTER_CRITICAL(&rbMux);
    if (rbLen >= RB_SIZE) { memmove(rb, rb + 1, RB_SIZE - 1); rbLen = RB_SIZE - 1; }
    rb[rbLen++] = b;
    lastRxMs = millis();
    portEXIT_CRITICAL(&rbMux);
}

/* ── 帧解析 ─────────────────────────────── */
static bool applyFrame(const uint8_t *f) {
    if (f[0] != MAGIC0 || f[1] != MAGIC1) return false;
    if (f[2] != PROTO_VER || f[3] != CMD_MULTI) return false;
    if (crc8(&f[2], 13) != f[15]) return false;

    unsigned long now = millis();
    if (lastApplyMs && (now - lastApplyMs) < MIN_INTERVAL) return true;
    lastApplyMs = now;

    uint16_t bp  = (uint16_t)f[5]  | ((uint16_t)f[6] << 8);
    uint16_t brp = (uint16_t)f[7]  | ((uint16_t)f[8] << 8);
    if (bp < PERIOD_MIN) bp = PERIOD_MIN; if (bp > PERIOD_MAX) bp = PERIOD_MAX;
    if (brp < PERIOD_MIN) brp = PERIOD_MIN; if (brp > PERIOD_MAX) brp = PERIOD_MAX;

    portENTER_CRITICAL(&ledMux);
    blinkPer  = bp;
    breathPer = brp;
    for (int i = 0; i < 3; i++) {
        chMode[i] = (f[9+i] <= CH_BREATH) ? f[9+i] : CH_OFF;
        chDuty[i] = f[12+i];
    }
    portEXIT_CRITICAL(&ledMux);
    lastValidFrameMs = now;  /* 喂狗 */
    return true;
}

static void processBuf(uint8_t *wb, uint16_t *wl) {
    while (*wl >= FRAME_LEN) {
        size_t i = 0;
        while (i + FRAME_LEN <= *wl && !(wb[i] == MAGIC0 && wb[i+1] == MAGIC1)) i++;
        if (i) { memmove(wb, wb + i, *wl - i); *wl -= i; }
        if (*wl < FRAME_LEN) break;
        if (wb[2] != PROTO_VER || wb[3] != CMD_MULTI) { memmove(wb, wb+1, --(*wl)); continue; }
        if (!applyFrame(wb))                          { memmove(wb, wb+1, --(*wl)); continue; }
        memmove(wb, wb + FRAME_LEN, *wl - FRAME_LEN); *wl -= FRAME_LEN;
    }
    while (*wl > 0 && wb[0] != MAGIC0) { memmove(wb, wb+1, --(*wl)); }
}

static void drainRB(void) {
    uint8_t wb[RB_SIZE]; uint16_t wl = 0;
    portENTER_CRITICAL(&rbMux);
    wl = rbLen; if (wl > RB_SIZE) wl = RB_SIZE;
    if (wl) memcpy(wb, rb, wl);
    rbLen = 0;
    portEXIT_CRITICAL(&rbMux);
    processBuf(wb, &wl);
    if (wl) {
        portENTER_CRITICAL(&rbMux);
        for (uint16_t i = 0; i < wl; i++) {
            if (rbLen >= RB_SIZE) { memmove(rb, rb+1, RB_SIZE-1); rbLen = RB_SIZE-1; }
            rb[rbLen++] = wb[i];
        }
        portEXIT_CRITICAL(&rbMux);
    }
}

/* ── PWM 任务 ───────────────────────────── */
static void pwmTask(void *) {
    const TickType_t dt = pdMS_TO_TICKS(1);
    TickType_t wake = xTaskGetTickCount();
    int pins[3] = {PIN_G, PIN_Y, PIN_R};
    for (;;) {
        vTaskDelayUntil(&wake, dt);
        uint8_t m[3], d[3]; uint16_t bp, brp;
        portENTER_CRITICAL(&ledMux);
        for (int i = 0; i < 3; i++) { m[i] = chMode[i]; d[i] = chDuty[i]; }
        bp = blinkPer; brp = breathPer;
        portEXIT_CRITICAL(&ledMux);
        if (bp < PERIOD_MIN) bp = PERIOD_MIN; if (bp > PERIOD_MAX) bp = PERIOD_MAX;
        if (brp < PERIOD_MIN) brp = PERIOD_MIN; if (brp > PERIOD_MAX) brp = PERIOD_MAX;
        uint64_t us = (uint64_t)esp_timer_get_time();
        /* ── 通信看门狗：超时未收到帧则全灭 ── */
        unsigned long nowMs = millis();
        if (lastValidFrameMs > 0 && (nowMs - lastValidFrameMs) > WATCHDOG_MS) {
            for (int i = 0; i < 3; i++) { m[i] = CH_OFF; d[i] = 0; }
        }
        bool bon = ((us % ((uint64_t)bp * 1000)) * 2 < (uint64_t)bp * 1000);
        uint16_t bb = 0;
        { uint64_t sp = (uint64_t)brp * 1000; if (sp < 2000) sp = 2000;
          uint64_t x = ((us % sp) * 510) / sp;
          bool r = (x <= 255); uint16_t l = r ? (uint16_t)x : (uint16_t)(510 - x);
          bb = breathCurve(l, r); }
        for (int i = 0; i < 3; i++) {
            uint8_t v = 0;
            switch (m[i]) {
                case CH_SOLID:  v = d[i]; break;
                case CH_BLINK:  v = bon ? d[i] : 0; break;
                case CH_BREATH: v = (uint8_t)(((uint32_t)d[i] * bb + 127) / 255); break;
            }
            setVis(pins[i], v);
        }
    }
}

/* ── setup / loop ────────────────────────── */
void setup() {
    /* 1. 硬件安全初始化：先拉高 GPIO 再接入 PWM，避免浮空点亮 */
    pinMode(PIN_G, OUTPUT); digitalWrite(PIN_G, HIGH);
    pinMode(PIN_Y, OUTPUT); digitalWrite(PIN_Y, HIGH);
    pinMode(PIN_R, OUTPUT); digitalWrite(PIN_R, HIGH);

    /* 2. 启动串口和 PWM */
    Serial.begin(115200);
    pwmInit();

    /* 3. 上电指示：三灯全亮 2 秒 */
    portENTER_CRITICAL(&ledMux);
    for (int i = 0; i < 3; i++) { chMode[i] = CH_SOLID; chDuty[i] = 255; }
    portEXIT_CRITICAL(&ledMux);

    xTaskCreate(pwmTask, "pwm", TASK_STACK, nullptr, TASK_PRIO, nullptr);
    delay(2000);

    /* 4. 全灭，等待宿主指令 */
    portENTER_CRITICAL(&ledMux);
    for (int i = 0; i < 3; i++) { chMode[i] = CH_OFF; chDuty[i] = 0; }
    portEXIT_CRITICAL(&ledMux);

    Serial.println(F("VibeLight V2 ready")); Serial.flush();

    /* 5. 启动通信看门狗（从上电指示结束后计时） */
    lastValidFrameMs = millis();
}

void loop() {
    for (int n = 0; n < DRAIN_MAX && Serial.available() > 0; n++)
        rbPush((uint8_t)Serial.read());
    drainRB();
    yield();
}
