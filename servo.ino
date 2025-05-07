#include <Servo.h>

Servo myservo;
const int servoPin = 9;

// Функция для тестового движения
void servoTest() {
  myservo.write(0);    // Исходная позиция
  delay(500);
  myservo.write(90);   // Поворот на 90°
  delay(500);
  myservo.write(180);  // Поворот на 180°
  delay(500);
  myservo.write(0);    // Возврат в 0°
  delay(500);
}

void setup() {
  Serial.begin(9600);
  myservo.attach(servoPin);

  // Добавлена только эта часть
  servoTest();         // Тест сервы при запуске
  Serial.println("Сервопривод протестирован!");
}

// Остальной код БЕЗ ИЗМЕНЕНИЙ
void loop() {
  if (Serial.available() > 0) {
    String command = Serial.readStringUntil('\n');
    command.trim();

    if (command == "OPEN") {
      myservo.write(90);
      delay(10000);
      myservo.write(0);
    }
  }
}