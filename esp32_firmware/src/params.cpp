#include "params.h"

namespace {
template <typename T>
bool applyIfPresent(JsonObjectConst input, const char *name, T &target, T minValue, T maxValue, String &error) {
  if (!input.containsKey(name)) {
    return true;
  }
  T value = input[name].as<T>();
  if (value < minValue || value > maxValue) {
    error = String(name) + " outside allowed range";
    return false;
  }
  target = value;
  return true;
}
}  // namespace

namespace Params {
bool applyJsonParams(JsonObjectConst input, RuntimeParams &params, String &error) {
  if (input.isNull()) {
    error = "params must be an object";
    return false;
  }

  bool ok = true;
  ok &= applyIfPresent<float>(input, "base_speed", params.base_speed, 0.10f, 0.45f, error);
  ok &= applyIfPresent<float>(input, "turn_speed", params.turn_speed, 0.08f, 0.35f, error);
  ok &= applyIfPresent<float>(input, "max_speed", params.max_speed, 0.10f, 0.60f, error);
  ok &= applyIfPresent<int>(input, "min_pwm_left", params.min_pwm_left, 0, 255, error);
  ok &= applyIfPresent<int>(input, "min_pwm_right", params.min_pwm_right, 0, 255, error);
  ok &= applyIfPresent<int>(input, "max_pwm", params.max_pwm, 80, 255, error);
  ok &= applyIfPresent<float>(input, "left_trim", params.left_trim, 0.70f, 1.30f, error);
  ok &= applyIfPresent<float>(input, "right_trim", params.right_trim, 0.70f, 1.30f, error);
  ok &= applyIfPresent<long>(input, "cell_ticks", params.cell_ticks, 200L, 5000L, error);
  ok &= applyIfPresent<long>(input, "turn_90_ticks", params.turn_90_ticks, 100L, 2500L, error);
  ok &= applyIfPresent<long>(input, "turn_180_ticks", params.turn_180_ticks, 200L, 5000L, error);
  ok &= applyIfPresent<long>(input, "brake_ticks", params.brake_ticks, 0L, 500L, error);
  ok &= applyIfPresent<long>(input, "stop_tolerance_ticks", params.stop_tolerance_ticks, 1L, 200L, error);
  ok &= applyIfPresent<float>(input, "speed_kp", params.speed_kp, 0.0f, 2.0f, error);
  ok &= applyIfPresent<float>(input, "speed_ki", params.speed_ki, 0.0f, 1.0f, error);
  ok &= applyIfPresent<float>(input, "speed_kd", params.speed_kd, 0.0f, 0.5f, error);
  ok &= applyIfPresent<float>(input, "heading_kp", params.heading_kp, 0.0f, 2.0f, error);
  ok &= applyIfPresent<float>(input, "heading_kd", params.heading_kd, 0.0f, 0.5f, error);
  ok &= applyIfPresent<int>(input, "wall_threshold_mm", params.wall_threshold_mm, 80, 260, error);
  ok &= applyIfPresent<int>(input, "open_threshold_mm", params.open_threshold_mm, 120, 500, error);
  ok &= applyIfPresent<int>(input, "front_stop_mm", params.front_stop_mm, 80, 250, error);
  ok &= applyIfPresent<int>(input, "danger_stop_mm", params.danger_stop_mm, 30, 120, error);
  ok &= applyIfPresent<int>(input, "filter_window", params.filter_window, 1, 15, error);
  ok &= applyIfPresent<float>(input, "center_kp", params.center_kp, 0.0f, 0.05f, error);
  ok &= applyIfPresent<float>(input, "center_max_correction", params.center_max_correction, 0.0f, 0.25f, error);
  ok &= applyIfPresent<uint32_t>(input, "heartbeat_timeout_ms", params.heartbeat_timeout_ms, 100UL, 3000UL, error);
  ok &= applyIfPresent<uint32_t>(input, "action_timeout_ms", params.action_timeout_ms, 1000UL, 30000UL, error);

  if (input.containsKey("wall_follow_enabled")) {
    params.wall_follow_enabled = input["wall_follow_enabled"].as<bool>();
  }
  if (input.containsKey("param_version")) {
    params.param_version = input["param_version"].as<uint32_t>();
  } else if (ok) {
    params.param_version++;
  }
  return ok;
}

int speedToPwm(float requestedSpeed, const RuntimeParams &params, bool leftSide) {
  float limitedSpeed = constrain(requestedSpeed, 0.0f, params.max_speed);
  float ratio = params.max_speed <= 0.0f ? 0.0f : limitedSpeed / params.max_speed;
  int minPwm = leftSide ? params.min_pwm_left : params.min_pwm_right;
  float trim = leftSide ? params.left_trim : params.right_trim;
  int pwm = minPwm + static_cast<int>((params.max_pwm - minPwm) * ratio);
  return constrain(static_cast<int>(pwm * trim), 0, params.max_pwm);
}
}  // namespace Params

