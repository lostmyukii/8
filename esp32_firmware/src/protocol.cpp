#include "protocol.h"

#include "config.h"

namespace {
void writeJsonLine(Stream &stream, JsonDocument &doc) {
  serializeJson(doc, stream);
  stream.print('\n');
}
}  // namespace

namespace Protocol {
void sendReady(Stream &stream, const ImuSnapshot &imu) {
  StaticJsonDocument<256> doc;
  doc["type"] = "ready";
  doc["fw"] = Config::FW_NAME;
  doc["version"] = Config::FW_VERSION;
  doc["imu_available"] = imu.available;
  JsonArray features = doc.createNestedArray("features");
  features.add("motor");
  features.add("encoder");
  features.add("tof");
  features.add("json_serial");
  features.add("imu_optional");
  writeJsonLine(stream, doc);
}

void sendAck(Stream &stream, int seq, bool ok, const String &message) {
  StaticJsonDocument<192> doc;
  doc["type"] = "ack";
  doc["seq"] = seq;
  doc["ok"] = ok;
  if (message.length() > 0) {
    doc["message"] = message;
  }
  writeJsonLine(stream, doc);
}

void sendTelemetry(
    Stream &stream,
    uint32_t uptimeMs,
    MotionState state,
    const SensorSnapshot &sensors,
    const ImuSnapshot &imu,
    long encLeft,
    long encRight,
    int pwmLeft,
    int pwmRight,
    uint32_t paramVersion) {
  StaticJsonDocument<512> doc;
  doc["type"] = "telemetry";
  doc["uptime_ms"] = uptimeMs;
  switch (state) {
    case MotionState::IDLE:
      doc["state"] = "IDLE";
      break;
    case MotionState::MOVING_CELL:
      doc["state"] = "MOVING_CELL";
      break;
    case MotionState::TURNING_LEFT:
      doc["state"] = "TURNING_LEFT";
      break;
    case MotionState::TURNING_RIGHT:
      doc["state"] = "TURNING_RIGHT";
      break;
    case MotionState::TURNING_BACK:
      doc["state"] = "TURNING_BACK";
      break;
    case MotionState::ESTOP:
      doc["state"] = "ESTOP";
      break;
    case MotionState::ERROR:
      doc["state"] = "ERROR";
      break;
  }
  doc["front_mm"] = sensors.front_mm;
  doc["left_mm"] = sensors.left_mm;
  doc["right_mm"] = sensors.right_mm;
  doc["enc_left"] = encLeft;
  doc["enc_right"] = encRight;
  doc["pwm_left"] = pwmLeft;
  doc["pwm_right"] = pwmRight;
  doc["param_version"] = paramVersion;
  doc["imu_available"] = imu.available;
  doc["imu_quality"] = imu.quality;
  if (imu.available) {
    doc["imu_yaw_deg"] = imu.yaw_deg;
    doc["yaw_rate_dps"] = imu.yaw_rate_dps;
    doc["accel_forward_mps2"] = imu.accel_forward_mps2;
  }
  writeJsonLine(stream, doc);
}

void sendDone(Stream &stream, const MotionResult &result, const SensorSnapshot &sensors) {
  StaticJsonDocument<384> doc;
  doc["type"] = "done";
  doc["action_id"] = result.action_id;
  doc["name"] = result.name;
  doc["success"] = result.success;
  doc["duration_ms"] = result.duration_ms;
  doc["enc_left"] = result.enc_left;
  doc["enc_right"] = result.enc_right;
  doc["front_mm"] = sensors.front_mm;
  doc["left_mm"] = sensors.left_mm;
  doc["right_mm"] = sensors.right_mm;
  writeJsonLine(stream, doc);
}

void sendError(Stream &stream, const String &actionId, const char *code, const char *message, const SensorSnapshot &sensors) {
  StaticJsonDocument<384> doc;
  doc["type"] = "error";
  if (actionId.length() > 0) {
    doc["action_id"] = actionId;
  }
  doc["code"] = code;
  doc["message"] = message;
  doc["front_mm"] = sensors.front_mm;
  doc["left_mm"] = sensors.left_mm;
  doc["right_mm"] = sensors.right_mm;
  writeJsonLine(stream, doc);
}
}  // namespace Protocol
