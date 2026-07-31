#include "safety.h"

SafetyGuard safetyGuard;

void SafetyGuard::begin(uint32_t nowMs) {
  lastHeartbeatMs_ = nowMs;
  seenHeartbeat_ = false;
}

void SafetyGuard::heartbeat(uint32_t nowMs) {
  lastHeartbeatMs_ = nowMs;
  seenHeartbeat_ = true;
}

bool SafetyGuard::heartbeatExpired(const RuntimeParams &params, uint32_t nowMs) const {
  if (!seenHeartbeat_) {
    return false;
  }
  return nowMs - lastHeartbeatMs_ > params.heartbeat_timeout_ms;
}

bool SafetyGuard::hasHeartbeat() const {
  return seenHeartbeat_;
}

