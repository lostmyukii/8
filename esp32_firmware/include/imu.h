#pragma once

#include <Arduino.h>

struct ImuSnapshot {
  bool available = false;
  float yaw_deg = 0.0f;
  float yaw_rate_dps = 0.0f;
  float accel_forward_mps2 = 0.0f;
  const char *quality = "not_configured";
};

class ImuSource {
 public:
  // The first implementation is deliberately hardware-neutral. A concrete
  // driver may set available=true only after its module and I2C pins have
  // been verified on the real car.
  bool begin();
  void tick(uint32_t nowMs);
  ImuSnapshot snapshot() const;

 private:
  ImuSnapshot snapshot_;
};

extern ImuSource imuSource;
