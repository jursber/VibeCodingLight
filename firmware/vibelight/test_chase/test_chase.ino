// 引脚识别测试
// GPIO 0 = 绿(常亮)  GPIO 1 = 黄(呼吸)  GPIO 2 = 红(快闪)

void setup() {
    ledcAttach(0, 5000, 8);
    ledcAttach(1, 5000, 8);
    ledcAttach(2, 5000, 8);
}

void loop() {
    unsigned long t = millis();

    // 绿灯：常亮 (active low: 0=最亮)
    ledcWrite(0, 0);

    // 黄灯：呼吸 (1.5秒周期)
    {
        unsigned long ph = t % 1500;
        uint16_t x = (ph < 750) ? (ph * 255 / 750) : ((1500 - ph) * 255 / 750);
        uint32_t sq = (uint32_t)x * x;
        uint16_t val = (uint16_t)((sq + 127) / 255);  // 感知呼吸曲线
        ledcWrite(1, 255 - val);
    }

    // 红灯：快闪 (200ms亮/200ms灭)
    bool on = ((t % 400) < 200);
    ledcWrite(2, on ? 0 : 255);

    delay(10);
}
