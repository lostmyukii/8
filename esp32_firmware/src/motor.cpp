#include "motor.h"

#include <Arduino.h>

#include "config.h"

MotorDriver motors;

namespace {
constexpr uint8_t LEFT_REV_CH = 0;
constexpr uint8_t LEFT_FWD_CH = 1;
constexpr uint8_t RIGHT_REV_CH = 2;
constexpr uint8_t RIGHT_FWD_CH = 3;
}  // namespace

void MotorDriver::begin() {
  ledcSetup(LEFT_REV_CH, Config::PWM_FREQ_HZ, Config::PWM_BITS);
  ledcSetup(LEFT_FWD_CH, Config::PWM_FREQ_HZ, Config::PWM_BITS);
  ledcSetup(RIGHT_REV_CH, Config::PWM_FREQ_HZ, Config::PWM_BITS);
  ledcSetup(RIGHT_FWD_CH, Config::PWM_FREQ_HZ, Config::PWM_BITS);

  ledcAttachPin(Config::LEFT_MOTOR_REV_PIN, LEFT_REV_CH);
  ledcAttachPin(Config::LEFT_MOTOR_FWD_PIN, LEFT_FWD_CH);
  ledcAttachPin(Config::RIGHT_MOTOR_REV_PIN, RIGHT_REV_CH);
  ledcAttachPin(Config::RIGHT_MOTOR_FWD_PIN, RIGHT_FWD_CH);
  stop();
}

void MotorDriver::drive(int leftPwmSigned, int rightPwmSigned) {
  writeMotor(LEFT_FWD_CH, LEFT_REV_CH, leftPwmSigned);
  writeMotor(RIGHT_FWD_CH, RIGHT_REV_CH, rightPwmSigned);
}

void MotorDriver::stop() {
  ledcWrite(LEFT_REV_CH, 0);
  ledcWrite(LEFT_FWD_CH, 0);
  ledcWrite(RIGHT_REV_CH, 0);
  ledcWrite(RIGHT_FWD_CH, 0);
}

void MotorDriver::writeMotor(uint8_t forwardChannel, uint8_t reverseChannel, int pwmSigned) {
  int pwm = constrain(abs(pwmSigned), 0, Config::PWM_MAX_10BIT);
  if (pwmSigned > 0) {
    ledcWrite(reverseChannel, 0);
    ledcWrite(forwardChannel, pwm);
  } else if (pwmSigned < 0) {
    ledcWrite(forwardChannel, 0);
    ledcWrite(reverseChannel, pwm);
  } else {
    ledcWrite(forwardChannel, 0);
    ledcWrite(reverseChannel, 0);
  }
}

