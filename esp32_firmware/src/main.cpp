#include <Arduino.h>
#include <ArduinoJson.h>

#include "config.h"
#include "encoder.h"
#include "imu.h"
#include "motion_controller.h"
#include "motor.h"
#include "params.h"
#include "protocol.h"
#include "safety.h"
#include "tof_sensors.h"

RuntimeParams runtimeParams;

namespace {
String serialLine;
uint32_t lastTelemetryMs = 0;
uint32_t lastMotionTickMs = 0;
bool heartbeatTimeoutReported = false;
int lastPwmLeft = 0;
int lastPwmRight = 0;

void handleMessage(const String &line) {
  StaticJsonDocument<768> doc;
  DeserializationError jsonError = deserializeJson(doc, line);
  SensorSnapshot sensors = tofSensors.snapshot();
  if (jsonError) {
    Protocol::sendError(Serial, "", "BAD_JSON", jsonError.c_str(), sensors);
    return;
  }

  const char *type = doc["type"] | "";
  int seq = doc["seq"] | -1;

  if (strcmp(type, "heartbeat") == 0) {
    safetyGuard.heartbeat(millis());
    heartbeatTimeoutReported = false;
    Protocol::sendAck(Serial, seq, true);
    return;
  }

  if (strcmp(type, "set_params") == 0) {
    String error;
    bool ok = Params::applyJsonParams(doc["params"].as<JsonObjectConst>(), runtimeParams, error);
    Protocol::sendAck(Serial, seq, ok, error);
    return;
  }

  if (strcmp(type, "action") == 0) {
    ActionCommand command;
    command.action_id = String(doc["action_id"] | "");
    command.name = String(doc["name"] | "");
    command.speed = doc["speed"] | 0.0f;
    command.target_ticks = doc["target_ticks"] | 0L;
    command.recovery = doc["recovery"] | false;
    command.direction = String(doc["direction"] | "");
    command.parent_action_id = String(doc["parent_action_id"] | "");

    String error;
    bool ok = motionController.start(command, runtimeParams, millis(), error);
    if (!ok) {
      motors.stop();
    }
    Protocol::sendAck(Serial, seq, ok, error);
    return;
  }

  if (strcmp(type, "stop") == 0) {
    motionController.stop();
    Protocol::sendAck(Serial, seq, true);
    return;
  }

  if (strcmp(type, "estop") == 0) {
    motionController.estop();
    Protocol::sendAck(Serial, seq, true);
    return;
  }

  if (strcmp(type, "clear_estop") == 0) {
    bool ok = motionController.clearEstop();
    Protocol::sendAck(Serial, seq, ok, ok ? "" : "estop was not active");
    return;
  }

  Protocol::sendAck(Serial, seq, false, "unknown message type");
}

void pollSerial() {
  while (Serial.available() > 0) {
    char ch = static_cast<char>(Serial.read());
    if (ch == '\n') {
      String line = serialLine;
      serialLine = "";
      line.trim();
      if (line.length() > 0) {
        handleMessage(line);
      }
    } else if (ch != '\r') {
      serialLine += ch;
      if (serialLine.length() > 768) {
        serialLine = "";
        Protocol::sendError(Serial, "", "LINE_TOO_LONG", "serial line exceeded 768 bytes", tofSensors.snapshot());
      }
    }
  }
}
}  // namespace

void setup() {
  Serial.begin(Config::SERIAL_BAUD);
  pinMode(Config::STATUS_LED_PIN, OUTPUT);
  digitalWrite(Config::STATUS_LED_PIN, HIGH);

  motors.begin();
  Encoder::begin();
  imuSource.begin();
  safetyGuard.begin(millis());

  String tofError;
  if (!tofSensors.begin(tofError)) {
    Protocol::sendError(Serial, "", "TOF_INIT_FAILED", tofError.c_str(), tofSensors.snapshot());
  }

  Protocol::sendReady(Serial, imuSource.snapshot());
}

void loop() {
  uint32_t now = millis();
  pollSerial();
  tofSensors.tick(now);
  imuSource.tick(now);

  if (safetyGuard.heartbeatExpired(runtimeParams, now)) {
    if (!heartbeatTimeoutReported) {
      motionController.stop();
      Protocol::sendError(Serial, "", "HEARTBEAT_TIMEOUT", "RDK heartbeat timeout", tofSensors.snapshot());
      heartbeatTimeoutReported = true;
    }
  }

  if (now - lastMotionTickMs >= Config::MOTION_TICK_MS) {
    lastMotionTickMs = now;
    MotionResult result = motionController.tick(runtimeParams, tofSensors.snapshot(), now);
    if (result.available) {
      if (result.success) {
        Protocol::sendDone(Serial, result, tofSensors.snapshot());
      } else {
        Protocol::sendMotionError(Serial, result, tofSensors.snapshot());
      }
    }
  }

  if (now - lastTelemetryMs >= Config::TELEMETRY_INTERVAL_MS) {
    lastTelemetryMs = now;
    Protocol::sendTelemetry(
        Serial,
        now,
        motionController.state(),
        tofSensors.snapshot(),
        imuSource.snapshot(),
        Encoder::leftCount(),
        Encoder::rightCount(),
        lastPwmLeft,
        lastPwmRight,
        runtimeParams.param_version);
  }
}
