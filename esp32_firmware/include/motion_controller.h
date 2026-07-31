#pragma once

#include <Arduino.h>

#include "params.h"
#include "tof_sensors.h"

enum class MotionState {
  IDLE,
  MOVING_CELL,
  TURNING_LEFT,
  TURNING_RIGHT,
  TURNING_BACK,
  ESTOP,
  ERROR
};

struct ActionCommand {
  String action_id;
  String name;
  float speed = 0.0f;
  long target_ticks = 0;
};

struct MotionResult {
  bool available = false;
  bool success = false;
  String action_id;
  String name;
  const char *error_code = "";
  const char *message = "";
  uint32_t duration_ms = 0;
  long enc_left = 0;
  long enc_right = 0;
};

class MotionController {
 public:
  bool start(const ActionCommand &command, const RuntimeParams &params, uint32_t nowMs, String &error);
  MotionResult tick(const RuntimeParams &params, const SensorSnapshot &sensors, uint32_t nowMs);
  void stop();
  void estop();
  bool clearEstop();
  MotionState state() const;
  const char *stateName() const;
  bool isBusy() const;

 private:
  MotionResult finish(bool success, const char *errorCode, const char *message, uint32_t nowMs);
  long targetTicksFor(const ActionCommand &command, const RuntimeParams &params) const;
  void driveForState(const RuntimeParams &params);

  MotionState state_ = MotionState::IDLE;
  ActionCommand active_;
  uint32_t actionStartMs_ = 0;
  long startLeft_ = 0;
  long startRight_ = 0;
};

extern MotionController motionController;

