#pragma once

#include <Arduino.h>
#include <ArduinoJson.h>

#include "motion_controller.h"
#include "params.h"
#include "tof_sensors.h"

namespace Protocol {
void sendReady(Stream &stream);
void sendAck(Stream &stream, int seq, bool ok, const String &message = "");
void sendTelemetry(
    Stream &stream,
    MotionState state,
    const SensorSnapshot &sensors,
    long encLeft,
    long encRight,
    int pwmLeft,
    int pwmRight,
    uint32_t paramVersion);
void sendDone(Stream &stream, const MotionResult &result, const SensorSnapshot &sensors);
void sendError(Stream &stream, const String &actionId, const char *code, const char *message, const SensorSnapshot &sensors);
}  // namespace Protocol

