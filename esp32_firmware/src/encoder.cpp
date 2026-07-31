#include "encoder.h"

#include "config.h"

namespace {
volatile long leftCountValue = 0;
volatile long rightCountValue = 0;

void IRAM_ATTR leftIsr() {
  int a = digitalRead(Config::LEFT_ENCODER_A_PIN);
  int b = digitalRead(Config::LEFT_ENCODER_B_PIN);
  if (a == 1) {
    leftCountValue += (b == 0) ? -1 : 1;
  } else {
    leftCountValue += (b == 1) ? -1 : 1;
  }
}

void IRAM_ATTR rightIsr() {
  int a = digitalRead(Config::RIGHT_ENCODER_A_PIN);
  int b = digitalRead(Config::RIGHT_ENCODER_B_PIN);
  if (a == 1) {
    rightCountValue += (b == 0) ? 1 : -1;
  } else {
    rightCountValue += (b == 1) ? -1 : 1;
  }
}
}  // namespace

namespace Encoder {
void begin() {
  pinMode(Config::LEFT_ENCODER_A_PIN, INPUT);
  pinMode(Config::LEFT_ENCODER_B_PIN, INPUT);
  pinMode(Config::RIGHT_ENCODER_A_PIN, INPUT);
  pinMode(Config::RIGHT_ENCODER_B_PIN, INPUT);
  attachInterrupt(digitalPinToInterrupt(Config::LEFT_ENCODER_A_PIN), leftIsr, CHANGE);
  attachInterrupt(digitalPinToInterrupt(Config::RIGHT_ENCODER_A_PIN), rightIsr, CHANGE);
}

void reset() {
  noInterrupts();
  leftCountValue = 0;
  rightCountValue = 0;
  interrupts();
}

long leftCount() {
  noInterrupts();
  long value = leftCountValue;
  interrupts();
  return value;
}

long rightCount() {
  noInterrupts();
  long value = rightCountValue;
  interrupts();
  return value;
}
}  // namespace Encoder

