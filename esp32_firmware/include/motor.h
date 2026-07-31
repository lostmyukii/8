#pragma once

#include <Arduino.h>

class MotorDriver {
 public:
  void begin();
  void drive(int leftPwmSigned, int rightPwmSigned);
  void stop();

 private:
  void writeMotor(uint8_t forwardChannel, uint8_t reverseChannel, int pwmSigned);
};

extern MotorDriver motors;

