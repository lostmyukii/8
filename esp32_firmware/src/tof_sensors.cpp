#include "tof_sensors.h"

#include <Wire.h>

#include "config.h"

ToFSensors tofSensors;

bool ToFSensors::begin(String &error) {
  Wire.begin();
  pinMode(Config::VL_FRONT_XSHUT_PIN, OUTPUT);
  pinMode(Config::VL_RIGHT_XSHUT_PIN, OUTPUT);
  pinMode(Config::VL_LEFT_XSHUT_PIN, OUTPUT);

  digitalWrite(Config::VL_FRONT_XSHUT_PIN, LOW);
  digitalWrite(Config::VL_RIGHT_XSHUT_PIN, LOW);
  digitalWrite(Config::VL_LEFT_XSHUT_PIN, LOW);
  delay(20);

  if (!initOne(front_, Config::VL_FRONT_XSHUT_PIN, Config::VL_FRONT_ADDR, "front", error)) {
    return false;
  }
  if (!initOne(right_, Config::VL_RIGHT_XSHUT_PIN, Config::VL_RIGHT_ADDR, "right", error)) {
    return false;
  }
  if (!initOne(left_, Config::VL_LEFT_XSHUT_PIN, Config::VL_LEFT_ADDR, "left", error)) {
    return false;
  }
  return true;
}

void ToFSensors::tick(uint32_t nowMs) {
  if (nowMs - lastReadMs_ < Config::TOF_INTERVAL_MS) {
    return;
  }
  lastReadMs_ = nowMs;
  snapshot_.front_mm = front_.readRangeContinuousMillimeters();
  snapshot_.right_mm = right_.readRangeContinuousMillimeters();
  snapshot_.left_mm = left_.readRangeContinuousMillimeters();
  snapshot_.front_ok = !front_.timeoutOccurred();
  snapshot_.right_ok = !right_.timeoutOccurred();
  snapshot_.left_ok = !left_.timeoutOccurred();
}

SensorSnapshot ToFSensors::snapshot() const {
  return snapshot_;
}

bool ToFSensors::initOne(VL53L0X &sensor, uint8_t xshutPin, uint8_t address, const char *name, String &error) {
  digitalWrite(xshutPin, HIGH);
  delay(20);
  sensor.setTimeout(100);
  if (!sensor.init()) {
    error = String("VL53 init failed: ") + name;
    return false;
  }
  sensor.setAddress(address);
  sensor.startContinuous(Config::TOF_INTERVAL_MS);
  return true;
}

