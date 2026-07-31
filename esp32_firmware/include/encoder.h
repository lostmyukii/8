#pragma once

#include <Arduino.h>

namespace Encoder {
void begin();
void reset();
long leftCount();
long rightCount();
}  // namespace Encoder

