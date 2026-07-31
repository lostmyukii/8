#include "motion_controller.h"

#include "encoder.h"
#include "motor.h"

MotionController motionController;

bool MotionController::start(const ActionCommand &command, const RuntimeParams &params, uint32_t nowMs, String &error) {
  if (state_ == MotionState::ESTOP) {
    error = "estop is active";
    return false;
  }
  if (isBusy()) {
    error = "motion controller is busy";
    return false;
  }
  if (command.name != "move_cell" && command.name != "turn_left" && command.name != "turn_right" &&
      command.name != "turn_back") {
    error = "unsupported action";
    return false;
  }

  active_ = command;
  if (active_.target_ticks <= 0) {
    active_.target_ticks = targetTicksFor(command, params);
  }
  actionStartMs_ = nowMs;
  Encoder::reset();
  startLeft_ = Encoder::leftCount();
  startRight_ = Encoder::rightCount();

  if (command.name == "move_cell") {
    state_ = MotionState::MOVING_CELL;
  } else if (command.name == "turn_left") {
    state_ = MotionState::TURNING_LEFT;
  } else if (command.name == "turn_right") {
    state_ = MotionState::TURNING_RIGHT;
  } else {
    state_ = MotionState::TURNING_BACK;
  }
  driveForState(params);
  return true;
}

MotionResult MotionController::tick(const RuntimeParams &params, const SensorSnapshot &sensors, uint32_t nowMs) {
  if (!isBusy()) {
    return MotionResult{};
  }
  if (state_ == MotionState::MOVING_CELL && sensors.front_ok && sensors.front_mm < params.danger_stop_mm) {
    return finish(false, "OBSTACLE_TOO_CLOSE", "front distance below danger_stop_mm", nowMs);
  }
  if (nowMs - actionStartMs_ > params.action_timeout_ms) {
    return finish(false, "ACTION_TIMEOUT", "action exceeded action_timeout_ms", nowMs);
  }

  long leftTravel = labs(Encoder::leftCount() - startLeft_);
  long rightTravel = labs(Encoder::rightCount() - startRight_);
  long averageTravel = (leftTravel + rightTravel) / 2;
  if (averageTravel >= active_.target_ticks - params.stop_tolerance_ticks) {
    return finish(true, "", "", nowMs);
  }

  driveForState(params);
  return MotionResult{};
}

void MotionController::stop() {
  motors.stop();
  state_ = MotionState::IDLE;
}

void MotionController::estop() {
  motors.stop();
  state_ = MotionState::ESTOP;
}

bool MotionController::clearEstop() {
  if (state_ != MotionState::ESTOP) {
    return false;
  }
  state_ = MotionState::IDLE;
  return true;
}

MotionState MotionController::state() const {
  return state_;
}

const char *MotionController::stateName() const {
  switch (state_) {
    case MotionState::IDLE:
      return "IDLE";
    case MotionState::MOVING_CELL:
      return "MOVING_CELL";
    case MotionState::TURNING_LEFT:
      return "TURNING_LEFT";
    case MotionState::TURNING_RIGHT:
      return "TURNING_RIGHT";
    case MotionState::TURNING_BACK:
      return "TURNING_BACK";
    case MotionState::ESTOP:
      return "ESTOP";
    case MotionState::ERROR:
      return "ERROR";
  }
  return "UNKNOWN";
}

bool MotionController::isBusy() const {
  return state_ == MotionState::MOVING_CELL || state_ == MotionState::TURNING_LEFT ||
         state_ == MotionState::TURNING_RIGHT || state_ == MotionState::TURNING_BACK;
}

MotionResult MotionController::finish(bool success, const char *errorCode, const char *message, uint32_t nowMs) {
  motors.stop();
  MotionResult result;
  result.available = true;
  result.success = success;
  result.action_id = active_.action_id;
  result.name = active_.name;
  result.error_code = errorCode;
  result.message = message;
  result.duration_ms = nowMs - actionStartMs_;
  result.enc_left = Encoder::leftCount();
  result.enc_right = Encoder::rightCount();
  state_ = success ? MotionState::IDLE : MotionState::ERROR;
  if (!success) {
    motors.stop();
  }
  return result;
}

long MotionController::targetTicksFor(const ActionCommand &command, const RuntimeParams &params) const {
  if (command.name == "move_cell") {
    return params.cell_ticks;
  }
  if (command.name == "turn_back") {
    return params.turn_180_ticks;
  }
  return params.turn_90_ticks;
}

void MotionController::driveForState(const RuntimeParams &params) {
  int leftPwm = Params::speedToPwm(active_.speed > 0 ? active_.speed : params.base_speed, params, true);
  int rightPwm = Params::speedToPwm(active_.speed > 0 ? active_.speed : params.base_speed, params, false);

  if (state_ == MotionState::MOVING_CELL) {
    motors.drive(leftPwm, rightPwm);
  } else if (state_ == MotionState::TURNING_LEFT) {
    motors.drive(-leftPwm, rightPwm);
  } else if (state_ == MotionState::TURNING_RIGHT || state_ == MotionState::TURNING_BACK) {
    motors.drive(leftPwm, -rightPwm);
  }
}

