#pragma once

#include <Arduino.h>

#include "params.h"

class SafetyGuard {
 public:
  void begin(uint32_t nowMs);
  void heartbeat(uint32_t nowMs);
  bool heartbeatExpired(const RuntimeParams &params, uint32_t nowMs) const;
  bool hasHeartbeat() const;

 private:
  uint32_t lastHeartbeatMs_ = 0;
  bool seenHeartbeat_ = false;
};

extern SafetyGuard safetyGuard;

