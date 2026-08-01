#include "imu.h"

ImuSource imuSource;

bool ImuSource::begin() {
  snapshot_ = ImuSnapshot{};
  return false;
}

void ImuSource::tick(uint32_t nowMs) {
  (void)nowMs;
}

ImuSnapshot ImuSource::snapshot() const {
  return snapshot_;
}
