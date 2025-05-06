import cv2
import pytesseract
import numpy as np
import re
import time
import psycopg2
import serial
from threading import Thread
from queue import Queue
from contextlib import closing

# Настройки Tesseract
pytesseract.pytesseract.tesseract_cmd = r'D:\tesseract\tesseract.exe'

# Конфигурация PostgreSQL
DB_CONFIG = {
    "dbname": "anpr_db",
    "user": "postgres",
    "password": "12345",
    "host": "localhost",
    "port": "5432"
}

# Настройки Arduino
ARDUINO_PORT = 'COM5'
ARDUINO_BAUDRATE = 9600


class ANPRSystem:
    def __init__(self):
        self.frame_queue = Queue(maxsize=2)
        self.running = True
        self.last_recognized = ""
        self.conn = None
        self.ser = None

        # Инициализация видеозахвата
        self.cap = cv2.VideoCapture(0)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        self.cap.set(cv2.CAP_PROP_FPS, 30)

        # Подключение к БД и Arduino
        self.connect_to_db()
        self.connect_to_arduino()

    def connect_to_db(self):
        """Установка соединения с базой данных"""
        try:
            self.conn = psycopg2.connect(**DB_CONFIG)
            self.conn.autocommit = True

            with closing(self.conn.cursor()) as cursor:
                cursor.execute("""
                    SELECT EXISTS (
                        SELECT 1 
                        FROM information_schema.tables 
                        WHERE table_name = 'license_plates'
                    )
                """)
                exists = cursor.fetchone()[0]
                if not exists:
                    raise Exception("Таблица license_plates не найдена")

            print("Успешное подключение к базе данных")
        except Exception as e:
            print(f"Ошибка подключения к БД: {str(e)}")
            self.running = False

    def connect_to_arduino(self):
        """Установка соединения с Arduino"""
        try:
            self.ser = serial.Serial(
                ARDUINO_PORT,
                ARDUINO_BAUDRATE,
                timeout=1
            )
            time.sleep(2)
            print("Успешное подключение к Arduino")
        except Exception as e:
            print(f"Ошибка подключения к Arduino: {str(e)}")
            self.running = False

    def check_plate_in_db(self, plate_number):
        """Проверка наличия номера в базе данных"""
        if not self.conn or self.conn.closed:
            self.connect_to_db()

        try:
            with closing(self.conn.cursor()) as cursor:
                cursor.execute(
                    "SELECT 1 FROM license_plates WHERE plate_number = %s",
                    (plate_number,)
                )
                return cursor.fetchone() is not None
        except Exception as e:
            print(f"Ошибка запроса: {str(e)}")
            return False

    def preprocess_roi(self, roi):
        """Оптимизированная предобработка области номера"""
        kernel = np.array([[-1, -1, -1], [-1, 9, -1], [-1, -1, -1]])
        sharpened = cv2.filter2D(roi, -1, kernel)

        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        contrast = clahe.apply(sharpened)

        _, thresh = cv2.threshold(contrast, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        return thresh

    def capture_frames(self):
        """Захват кадров с камеры"""
        while self.running:
            ret, frame = self.cap.read()
            if ret and self.frame_queue.qsize() < 2:
                self.frame_queue.put(frame)

    def process_frames(self):
        """Обработка и распознавание кадров"""
        while self.running:
            if self.frame_queue.empty():
                time.sleep(0.01)
                continue

            frame = self.frame_queue.get()
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

            # Предобработка изображения
            blur = cv2.bilateralFilter(gray, 9, 75, 75)
            thresh = cv2.adaptiveThreshold(
                blur, 255,
                cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY_INV, 31, 5
            )

            # Поиск контуров
            contours, _ = cv2.findContours(
                thresh,
                cv2.RETR_EXTERNAL,
                cv2.CHAIN_APPROX_SIMPLE
            )

            plate_text = ""
            status = ""
            for cnt in sorted(contours, key=cv2.contourArea, reverse=True)[:5]:
                x, y, w, h = cv2.boundingRect(cnt)
                area = cv2.contourArea(cnt)

                if 2000 < area < 30000 and 2.5 < (w / h) < 5 and w > 100:
                    roi = gray[y:y + h, x:x + w]
                    roi = cv2.resize(roi, (w * 4, h * 4), interpolation=cv2.INTER_CUBIC)
                    processed = self.preprocess_roi(roi)

                    # Распознавание текста
                    text = pytesseract.image_to_string(
                        processed,
                        config='--oem 3 --psm 8 -l rus'
                    ).strip().upper()

                    # Коррекция символов
                    text = re.sub(r'[^А-ЯA-Z0-9]', '', text)
                    text = text.replace('0', 'О').replace('K', 'К').replace('Y', 'У')

                    if self.validate_plate(text):
                        status = "ПРОПУСК" if self.check_plate_in_db(text) else "НЕИЗВЕСТНЫЙ"
                        cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 255, 0), 3)
                        cv2.putText(frame, f"{text} - {status}",
                                    (x, y - 30), cv2.FONT_HERSHEY_SIMPLEX,
                                    0.9, (0, 255, 0), 2)
                        plate_text = text
                        break

            # Отправка команды ВНЕ цикла обработки контуров
            if plate_text and plate_text != self.last_recognized:
                print(f"Распознан: {plate_text} | Статус: {status}")
                self.last_recognized = plate_text

                if status == "ПРОПУСК" and self.ser and self.ser.is_open:
                    try:
                        self.ser.write(b'OPEN\n')
                        print("Команда OPEN отправлена на Arduino")
                    except Exception as e:
                        print(f"Ошибка отправки: {str(e)}")

            cv2.imshow('ANPR System', frame)
            if cv2.waitKey(1) & 0xFF == ord('q'):
                self.running = False

    def validate_plate(self, text):
        """Валидация формата номера"""
        pattern = r'^[АВЕКМНОРСТУХ]\d{3}[АВЕКМНОРСТУХ]{2}\d{2,3}$'
        return re.fullmatch(pattern, text, re.IGNORECASE)

    def run(self):
        """Запуск системы"""
        Thread(target=self.capture_frames, daemon=True).start()
        self.process_frames()
        self.cap.release()
        if self.conn and not self.conn.closed:
            self.conn.close()
        if self.ser and self.ser.is_open:
            self.ser.close()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    system = ANPRSystem()
    system.run()