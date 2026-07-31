#pragma once

#include <Arduino.h>

namespace Config {
constexpr uint32_t SERIAL_BAUD = 115200;
constexpr const char *FW_NAME = "maze-esp32";
constexpr const char *FW_VERSION = "0.1.0";

constexpr uint8_t STATUS_LED_PIN = 12;

constexpr uint8_t LEFT_MOTOR_REV_PIN = 2;
constexpr uint8_t LEFT_MOTOR_FWD_PIN = 4;
constexpr uint8_t RIGHT_MOTOR_REV_PIN = 13;
constexpr uint8_t RIGHT_MOTOR_FWD_PIN = 27;

constexpr uint8_t LEFT_ENCODER_A_PIN = 25;
constexpr uint8_t LEFT_ENCODER_B_PIN = 26;
constexpr uint8_t RIGHT_ENCODER_A_PIN = 16;
constexpr uint8_t RIGHT_ENCODER_B_PIN = 17;

constexpr uint8_t VL_FRONT_XSHUT_PIN = 18;
constexpr uint8_t VL_RIGHT_XSHUT_PIN = 19;
constexpr uint8_t VL_LEFT_XSHUT_PIN = 5;

constexpr uint8_t VL_FRONT_ADDR = 0x30;
constexpr uint8_t VL_RIGHT_ADDR = 0x31;
constexpr uint8_t VL_LEFT_ADDR = 0x32;

constexpr uint32_t MOTION_TICK_MS = 10;
constexpr uint32_t TELEMETRY_INTERVAL_MS = 100;
constexpr uint32_t TOF_INTERVAL_MS = 30;
constexpr int PWM_FREQ_HZ = 5000;
constexpr int PWM_BITS = 10;
constexpr int PWM_MAX_10BIT = 1023;
}  // namespace Config

