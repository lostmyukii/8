#include <Arduino.h>
#include <micro_ros_platformio.h>

#include <rcl/rcl.h>
#include <rclc/rclc.h>
#include <rclc/executor.h>

#include <std_msgs/msg/int32.h>
// 引入消息类型头文件
#include <std_msgs/msg/u_int16_multi_array.h>


#include <Wire.h>
#include <VL53L0X.h>

#define ARRAY_LEN 4
VL53L0X VLFront, VLBack, VLRight, VLLeft;
#define VL_FRONT 18
#define VL_BACK 23  
#define VL_RIGHT 19
#define VL_LEFT 5

#if !defined(MICRO_ROS_TRANSPORT_ARDUINO_SERIAL)
#error This example is only avaliable for Arduino framework with serial transport.
#endif

rcl_publisher_t publisher;
rcl_subscription_t subscriber;

//std_msgs__msg__Int32 msg;
rclc_executor_t executor;
rclc_support_t support;
rcl_allocator_t allocator;
rcl_node_t node;
rcl_timer_t timer;
std_msgs__msg__UInt16MultiArray msg;
std_msgs__msg__Int32 msg_sub;

const int left1 = 2;   // 左电机控制引脚1
const int left2 = 4;   // 左电机控制引脚2
const int right1 = 13; // 右电机控制引脚1
const int right2 = 27; // 右电机控制引脚2
int leftPWM=760;
int rightPWM=820;//由于两个马达粘滞不一样，所以起始速度不同
int dir=3;//方向  0-停 1-前进 2-左转 3-右转
int pt;//实验millis（）定义，可删掉
int lastAdjustTime=0,lastLeftCount=0,lastRightCount=0;
int leftsum=0,rightsum=0;
const int thrhold=2100,rhold=445,lhold=470, bhold =2100;//前进 右转 左转的码盘阈值

#define RCCHECK(fn) { rcl_ret_t temp_rc = fn; if((temp_rc != RCL_RET_OK)){error_loop();}}
#define RCSOFTCHECK(fn) { rcl_ret_t temp_rc = fn; if((temp_rc != RCL_RET_OK)){}}

void VL53L0X_Setup();
void move_setup();
void zright();
void zleft();
void sp();
void fw();
void tr();
void tl();
void bw();


// Error handle loop
void error_loop() {
  while(1) {
    delay(100);
  }
}

void timer_callback(rcl_timer_t * timer, int64_t last_call_time) {
  RCLC_UNUSED(last_call_time);
  if (timer != NULL) {
    msg.data.data[0] = (uint16_t)VLFront.readRangeContinuousMillimeters();
    msg.data.data[1] = (uint16_t)VLBack.readRangeContinuousMillimeters();
    msg.data.data[2] = (uint16_t)VLRight.readRangeContinuousMillimeters();
    msg.data.data[3] = (uint16_t)VLLeft.readRangeContinuousMillimeters();

    RCSOFTCHECK(rcl_publish(&publisher, &msg, NULL));
    //msg.data++;
  }
}

// --- 订阅者回调函数 (Int32) ---
void subscription_callback(const void * msgin)
{
  // 将传入的 void 指针转换为 Int32 消息指针
  const std_msgs__msg__Int32 * msg = (const std_msgs__msg__Int32 *)msgin;

  // 直接访问 msg->data (int32_t 类型)
  // 示例逻辑：如果收到的数值大于 100，点亮 LED
 if (msg->data > 100) {
    digitalWrite(12, HIGH);
  } else {
    digitalWrite(12, LOW);
  }
 if(msg->data == 1)
    fw();
 else if(msg->data == 2)
    bw();
 else if(msg->data == 3)
    tr();
 else if(msg->data == 4)
    tl();
 else
    sp();
}

void setup() {
  // Configure serial transport
  Serial.begin(115200);
  pinMode(12, OUTPUT);
  digitalWrite(12, HIGH);  
  
  VL53L0X_Setup();
  move_setup();

  set_microros_serial_transports(Serial);
  delay(2000);

  allocator = rcl_get_default_allocator();

  //create init_options
  RCCHECK(rclc_support_init(&support, 0, NULL, &allocator));

  // create node
  RCCHECK(rclc_node_init_default(&node, "micro_ros_platformio_node", "", &support));

  RCCHECK(rclc_publisher_init_default(
    &publisher,
    &node,
    ROSIDL_GET_MSG_TYPE_SUPPORT(std_msgs, msg, UInt16MultiArray),
    "micro_ros_uint16_array_publisher"));

  msg.data.capacity = ARRAY_LEN;
  msg.data.size = ARRAY_LEN;
  
  // 使用 micro-ROS 分配器分配内存 (4 * 2 bytes = 8 bytes)
  msg.data.data = (uint16_t*) allocator.allocate(ARRAY_LEN * sizeof(uint16_t), allocator.state);

  msg.layout.dim.size = 0;
  msg.layout.dim.capacity = 0;
  msg.layout.dim.data = NULL;

  RCCHECK(rclc_subscription_init_default(
    &subscriber,
    &node,
    ROSIDL_GET_MSG_TYPE_SUPPORT(std_msgs, msg, Int32),
    "micro_ros_int32_subscriber"));

  // create timer,
  const unsigned int timer_timeout = 10000;
  RCCHECK( rclc_timer_init_default(
    &timer,
    &support,
    RCL_MS_TO_NS(timer_timeout),
    timer_callback));

  // create executor
  RCCHECK(rclc_executor_init(&executor, &support.context, 2, &allocator));
  RCCHECK(rclc_executor_add_timer(&executor, &timer));

  //msg.data = 0;
  RCCHECK(rclc_executor_add_subscription(&executor, &subscriber, &msg_sub, &subscription_callback, ON_NEW_DATA));
}

void loop() {
  delay(100);
  RCSOFTCHECK(rclc_executor_spin_some(&executor, RCL_MS_TO_NS(100)));
}

void VL53L0X_Setup() {
  Wire.begin();
  pinMode(VL_FRONT,OUTPUT);
  digitalWrite(VL_FRONT,LOW);
  delay(10);                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         
  digitalWrite(VL_FRONT,HIGH);
  delay(10);
  VLFront.setAddress(0x30);
  pinMode(VL_BACK,OUTPUT);
  digitalWrite(VL_BACK,LOW);
  delay(10);
  digitalWrite(VL_BACK,HIGH);
  delay(10);
  VLBack.setAddress(0x31);
  pinMode(VL_RIGHT,OUTPUT);
  digitalWrite(VL_RIGHT,LOW);
  delay(10);
  digitalWrite(VL_RIGHT,HIGH);
  delay(10);
  VLRight.setAddress(0x32);
  pinMode(VL_LEFT,OUTPUT);
  digitalWrite(VL_LEFT,LOW);
  delay(10);
  digitalWrite(VL_LEFT,HIGH);
  delay(10);
  VLLeft.setAddress(0x33);
  VLFront.setTimeout(500);
  VLBack.setTimeout(500);
  VLRight.setTimeout(500);
  VLLeft.setTimeout(500);
  if (!VLFront.init())
  {
    Serial.println("传感器f初始化失败！");
    while (1) {}
  }
  if (!VLBack.init())
  {
    Serial.println("传感器b初始化失败！");
    while (1) {}
  }
  if (!VLRight.init())
  {
    Serial.println("传感器r初始化失败！");
    while (1) {}
  }
  if (!VLLeft.init())
  {
    Serial.println("传感器l初始化失败！");
    while (1) {}
  }
  
  // 启动连续测距模式
  VLFront.startContinuous();
  VLBack.startContinuous();
  VLRight.startContinuous();
  VLLeft.startContinuous();
}

void move_setup()
{
  pinMode(left1, OUTPUT);
  pinMode(left2, OUTPUT);
  pinMode(right1, OUTPUT);
  pinMode(right2, OUTPUT);

  pinMode(25, INPUT);
  pinMode(26, INPUT); 
  attachInterrupt(digitalPinToInterrupt(25),zleft,CHANGE);//左侧

  pinMode(16, INPUT);
  pinMode(17, INPUT); 
  attachInterrupt(digitalPinToInterrupt(16),zright,CHANGE);//右侧

  ledcSetup(0, 5000, 10);
  ledcSetup(1, 5000, 10);
  ledcSetup(2, 5000, 10);
  ledcSetup(3, 5000, 10);
  
  ledcAttachPin(2, 0);    
  ledcAttachPin(4, 1);  
  ledcAttachPin(13, 2);
  ledcAttachPin(27, 3); 
}

void zleft(){
  int a,b;
  a=digitalRead(25);
  b=digitalRead(26);
  if(a==1){
    if(b==0)leftsum--;
    else leftsum++;
  }
  else{
    if(b==1)leftsum--;
    else leftsum++;
  }
}//左侧中断

void zright(){
  int c,d;
  c=digitalRead(16);
  d=digitalRead(17);
  if(c==1){
    if(d==0)rightsum++;
    else rightsum--;
  }
  else{
    if(d==1)rightsum--;
    else rightsum++;
  }
}//右侧中断

void fw()
{
   leftsum=0;
   rightsum=0;
   while (leftsum<thrhold) {
    
   int now = millis();
  
      if (now - lastAdjustTime >= 2) {// 每 2ms 进行一次调整（可根据实际情况修改时间间隔）  
        int leftDelta = leftsum - lastLeftCount;
        int rightDelta = rightsum - lastRightCount;// 计算左右轮在时间间隔内的脉冲增量
        int error = leftDelta - rightDelta;// 速度误差 = 左轮增量 - 右轮增量
        int adjust = 2*error ;// 比例调节：误差为正表示左轮快，应减小左PWM或增大右PWM
    
        leftPWM -= adjust;
        rightPWM += adjust;// 调整PWM值
    
        if (leftPWM < 800) leftPWM = 800;
        if (leftPWM > 950) leftPWM = 950;
        if (rightPWM < 650) rightPWM = 650;
        if (rightPWM > 850) rightPWM = 850;// 限幅
    
        lastLeftCount = leftsum;
        lastRightCount = rightsum;
        lastAdjustTime = now;// 更新记录值
      }
      ledcWrite(0, 0);
      ledcWrite(1, leftPWM);
      ledcWrite(2, 0);
      ledcWrite(3, rightPWM); // 应用PWM 
    }
    leftsum=0;
    rightsum=0;
    sp();
}

void tr(){
  
    leftsum=0;
    rightsum=0;
     while(leftsum<lhold)
     {
        //右转 左-  右+
      int now = millis();
      if (now - lastAdjustTime >= 2) {// 每 2ms 进行一次调整（可根据实际情况修改时间间隔）  
        int rightsum1=-1*rightsum;
        int leftDelta = leftsum - lastLeftCount;
        int rightDelta = rightsum1 - lastRightCount;// 计算左右轮在时间间隔内的脉冲增量
        int error = leftDelta - rightDelta;// 速度误差 = 左轮增量 - 右轮增量
        int adjust = 2*error ;// 比例调节：误差为正表示左轮快，应减小左PWM或增大右PWM
    
        leftPWM -= adjust;
        rightPWM += adjust;// 调整PWM值
    
        if (leftPWM < 800) leftPWM = 800;
        if (leftPWM > 950) leftPWM = 950;
        if (rightPWM < 650) rightPWM = 650;
        if (rightPWM > 850) rightPWM = 850;// 限幅
    
        lastLeftCount = leftsum;
        lastRightCount = rightsum1;
        lastAdjustTime = now;// 更新记录值
      }
    
    ledcWrite(0, 0);
    ledcWrite(1, leftPWM);
    ledcWrite(2, rightPWM);
    ledcWrite(3, 0);  
  }
  leftsum=0;
  rightsum=0;
  sp();
}

void tl(){

    leftsum=0;
    rightsum=0;
   while(rightsum<lhold) 
  {
    //左转 左-  右+
      int now = millis();
      if (now - lastAdjustTime >= 2) {// 每 2ms 进行一次调整（可根据实际情况修改时间间隔）  
        int leftsum1=-1*leftsum;
        int leftDelta = leftsum1 - lastLeftCount;
        int rightDelta = rightsum - lastRightCount;// 计算左右轮在时间间隔内的脉冲增量
        int error = leftDelta - rightDelta;// 速度误差 = 左轮增量 - 右轮增量
        int adjust = 2*error ;// 比例调节：误差为正表示左轮快，应减小左PWM或增大右PWM
    
        leftPWM -= adjust;
        rightPWM += adjust;// 调整PWM值
    
        if (leftPWM < 800) leftPWM = 800;
        if (leftPWM > 950) leftPWM = 950;
        if (rightPWM < 650) rightPWM = 650;
        if (rightPWM > 850) rightPWM = 850;// 限幅
    
        lastLeftCount = leftsum1;
        lastRightCount = rightsum;
        lastAdjustTime = now;// 更新记录值
      }
    
    ledcWrite(0, leftPWM);
    ledcWrite(1, 0);
    ledcWrite(2, 0);
    ledcWrite(3, rightPWM);  
  }
  leftsum=0;
  rightsum=0;
  sp();
}

void bw(){

    leftsum=0;
    rightsum=0;
   while(abs(leftsum)<bhold) 
   {
      int now = millis();
      int leftsum1=-1*leftsum;
      int rightsum1=-1*rightsum;
      if (now - lastAdjustTime >= 2) {// 每 2ms 进行一次调整（可根据实际情况修改时间间隔）  
        int leftDelta = leftsum1 - lastLeftCount;
        int rightDelta = rightsum1 - lastRightCount;// 计算左右轮在时间间隔内的脉冲增量
        int error = leftDelta - rightDelta;// 速度误差 = 左轮增量 - 右轮增量
        int adjust = 2*error ;// 比例调节：误差为正表示左轮快，应减小左PWM或增大右PWM
    
        leftPWM -= adjust;
        rightPWM += adjust;// 调整PWM值
    
        if (leftPWM < 800) leftPWM = 800;
        if (leftPWM > 950) leftPWM = 950;
        if (rightPWM < 650) rightPWM = 650;
        if (rightPWM > 850) rightPWM = 850;// 限幅
    
        lastLeftCount = leftsum1;
        lastRightCount = rightsum1;
        lastAdjustTime = now;// 更新记录值
      }
      ledcWrite(0, leftPWM);
      ledcWrite(1, 0);
      ledcWrite(2, rightPWM);
      ledcWrite(3, 0); // 应用PWM 
  }
  leftsum=0;
  rightsum=0;
  sp();
}


void sp(){
  ledcWrite(0, 0);
  ledcWrite(1, 0);
  ledcWrite(2, 0);
  ledcWrite(3, 0);
}