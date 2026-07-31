#pragma once

#include <Arduino.h>
#include <ArduinoJson.h>

struct RuntimeParams {
  float base_speed = 0.25f;
  float turn_speed = 0.18f;
  float max_speed = 0.40f;
  int min_pwm_left = 45;
  int min_pwm_right = 45;
  int max_pwm = 180;
  float left_trim = 1.0f;
  float right_trim = 1.0f;

  long cell_ticks = 1350;
  long turn_90_ticks = 720;
  long turn_180_ticks = 1440;
  long brake_ticks = 30;
  long stop_tolerance_ticks = 20;

  float speed_kp = 0.8f;
  float speed_ki = 0.0f;
  float speed_kd = 0.05f;
  float heading_kp = 0.6f;
  float heading_kd = 0.03f;

  int wall_threshold_mm = 150;
  int open_threshold_mm = 220;
  int front_stop_mm = 120;
  int danger_stop_mm = 60;
  int filter_window = 5;

  bool wall_follow_enabled = true;
  float center_kp = 0.004f;
  float center_max_correction = 0.08f;

  uint32_t heartbeat_timeout_ms = 500;
  uint32_t action_timeout_ms = 8000;
  uint32_t param_version = 1;
};

namespace Params {
bool applyJsonParams(JsonObjectConst input, RuntimeParams &params, String &error);
int speedToPwm(float requestedSpeed, const RuntimeParams &params, bool leftSide);
}  // namespace Params

