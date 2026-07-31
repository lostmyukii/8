#pragma once

#include <Arduino.h>
#include <VL53L0X.h>

struct SensorSnapshot {
  uint16_t front_mm = 8190;
  uint16_t left_mm = 8190;
  uint16_t right_mm = 8190;
  bool front_ok = false;
  bool left_ok = false;
  bool right_ok = false;
};

class ToFSensors {
 public:
  bool begin(String &error);
  void tick(uint32_t nowMs);
  SensorSnapshot snapshot() const;

 private:
  bool initOne(VL53L0X &sensor, uint8_t xshutPin, uint8_t address, const char *name, String &error);

  VL53L0X front_;
  VL53L0X right_;
  VL53L0X left_;
  SensorSnapshot snapshot_;
  uint32_t lastReadMs_ = 0;
};

extern ToFSensors tofSensors;

