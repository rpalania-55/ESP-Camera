#include <Arduino.h>
#include <HTTPClient.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <esp_camera.h>
#include <esp_sleep.h>
#include <time.h>

#include "certificates.h"
#include "secrets.h"

// AI Thinker ESP32-CAM / common OV2640 4 MB PSRAM clone pin map.
#define PWDN_GPIO_NUM 32
#define RESET_GPIO_NUM -1
#define XCLK_GPIO_NUM 0
#define SIOD_GPIO_NUM 26
#define SIOC_GPIO_NUM 27
#define Y9_GPIO_NUM 35
#define Y8_GPIO_NUM 34
#define Y7_GPIO_NUM 39
#define Y6_GPIO_NUM 36
#define Y5_GPIO_NUM 21
#define Y4_GPIO_NUM 19
#define Y3_GPIO_NUM 18
#define Y2_GPIO_NUM 5
#define VSYNC_GPIO_NUM 25
#define HREF_GPIO_NUM 23
#define PCLK_GPIO_NUM 22
#define FLASH_GPIO_NUM 4

constexpr char TIMEZONE[] = "EST5EDT,M3.2.0/2,M11.1.0/2";
constexpr char NTP_SERVER_1[] = "time.google.com";
constexpr char NTP_SERVER_2[] = "pool.ntp.org";
constexpr int CAPTURE_HOURS[] = {8, 18};
constexpr uint64_t MICROSECONDS_PER_SECOND = 1000000ULL;
constexpr uint64_t RETRY_SLEEP_SECONDS = 15ULL * 60ULL;
constexpr bool USE_FLASH = false;
constexpr bool VERTICAL_FLIP = false;
constexpr bool HORIZONTAL_MIRROR = true;

enum class UploadResult { SUCCESS, PERMANENT_FAILURE, TRANSIENT_FAILURE };

void deepSleepFor(uint64_t seconds) {
  Serial.printf("Sleeping for %llu seconds\n", seconds);
  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);
  esp_sleep_enable_timer_wakeup(seconds * MICROSECONDS_PER_SECOND);
  Serial.flush();
  esp_deep_sleep_start();
}

bool connectWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("Connecting to Wi-Fi");
  const unsigned long deadline = millis() + 30000;
  while (WiFi.status() != WL_CONNECTED && millis() < deadline) {
    delay(500);
    Serial.print('.');
  }
  Serial.println();
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("Wi-Fi connection timed out");
    return false;
  }
  Serial.print("Wi-Fi connected: ");
  Serial.println(WiFi.localIP());
  return true;
}

bool synchronizeClock(struct tm &localTime) {
  configTzTime(TIMEZONE, NTP_SERVER_1, NTP_SERVER_2);
  if (!getLocalTime(&localTime, 20000)) {
    Serial.println("NTP synchronization timed out");
    return false;
  }
  char timestamp[40];
  strftime(timestamp, sizeof(timestamp), "%Y-%m-%dT%H:%M:%S%z", &localTime);
  Serial.printf("Clock synchronized: %s\n", timestamp);
  return true;
}

uint64_t secondsUntilNextCapture() {
  time_t now = time(nullptr);
  struct tm localNow;
  localtime_r(&now, &localNow);
  time_t next = 0;

  for (int dayOffset = 0; dayOffset <= 1; ++dayOffset) {
    for (int captureHour : CAPTURE_HOURS) {
      struct tm candidate = localNow;
      candidate.tm_mday += dayOffset;
      candidate.tm_hour = captureHour;
      candidate.tm_min = 0;
      candidate.tm_sec = 0;
      candidate.tm_isdst = -1;
      const time_t candidateEpoch = mktime(&candidate);
      if (candidateEpoch > now + 5 && (next == 0 || candidateEpoch < next)) {
        next = candidateEpoch;
      }
    }
  }

  if (next <= now) {
    return 12ULL * 60ULL * 60ULL;
  }
  return static_cast<uint64_t>(next - now);
}

bool initializeCamera() {
  camera_config_t config = {};
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer = LEDC_TIMER_0;
  config.pin_d0 = Y2_GPIO_NUM;
  config.pin_d1 = Y3_GPIO_NUM;
  config.pin_d2 = Y4_GPIO_NUM;
  config.pin_d3 = Y5_GPIO_NUM;
  config.pin_d4 = Y6_GPIO_NUM;
  config.pin_d5 = Y7_GPIO_NUM;
  config.pin_d6 = Y8_GPIO_NUM;
  config.pin_d7 = Y9_GPIO_NUM;
  config.pin_xclk = XCLK_GPIO_NUM;
  config.pin_pclk = PCLK_GPIO_NUM;
  config.pin_vsync = VSYNC_GPIO_NUM;
  config.pin_href = HREF_GPIO_NUM;
  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn = PWDN_GPIO_NUM;
  config.pin_reset = RESET_GPIO_NUM;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;
  config.frame_size = FRAMESIZE_UXGA;
  config.jpeg_quality = 10;
  config.fb_count = psramFound() ? 2 : 1;
  config.grab_mode = CAMERA_GRAB_LATEST;
  config.fb_location = psramFound() ? CAMERA_FB_IN_PSRAM : CAMERA_FB_IN_DRAM;

  const esp_err_t result = esp_camera_init(&config);
  if (result != ESP_OK) {
    Serial.printf("Camera initialization failed: 0x%x\n", result);
    return false;
  }

  sensor_t *sensor = esp_camera_sensor_get();
  sensor->set_vflip(sensor, VERTICAL_FLIP ? 1 : 0);
  sensor->set_hmirror(sensor, HORIZONTAL_MIRROR ? 1 : 0);
  return true;
}

camera_fb_t *capturePhoto() {
  for (int i = 0; i < 2; ++i) {
    camera_fb_t *warmup = esp_camera_fb_get();
    if (warmup != nullptr) {
      esp_camera_fb_return(warmup);
    }
    delay(150);
  }

  if (USE_FLASH) {
    pinMode(FLASH_GPIO_NUM, OUTPUT);
    digitalWrite(FLASH_GPIO_NUM, HIGH);
    delay(250);
  }
  camera_fb_t *photo = esp_camera_fb_get();
  if (USE_FLASH) {
    digitalWrite(FLASH_GPIO_NUM, LOW);
  }
  return photo;
}

UploadResult uploadPhoto(camera_fb_t *photo, const struct tm &capturedAt) {
  WiFiClientSecure client;
  client.setCACert(GOOGLE_ROOT_CA);
  HTTPClient http;
  http.setConnectTimeout(15000);
  http.setTimeout(45000);
  if (!http.begin(client, CLOUD_RUN_CAPTURE_URL)) {
    Serial.println("Could not initialize HTTPS request");
    return UploadResult::TRANSIENT_FAILURE;
  }

  char timestamp[40];
  strftime(timestamp, sizeof(timestamp), "%Y-%m-%dT%H:%M:%S%z", &capturedAt);
  http.addHeader("Content-Type", "image/jpeg");
  http.addHeader("X-Device-ID", DEVICE_ID);
  http.addHeader("X-Device-Token", DEVICE_TOKEN);
  http.addHeader("X-Captured-At", timestamp);
  const int status = http.POST(photo->buf, photo->len);
  const String response = status > 0 ? http.getString() : "";
  http.end();

  Serial.printf("Upload HTTP status: %d\n", status);
  if (response.length() > 0) {
    Serial.println(response);
  }
  if (status == 200) {
    return UploadResult::SUCCESS;
  }
  if (status == 400 || status == 401 || status == 413 || status == 415 ||
      status == 422) {
    return UploadResult::PERMANENT_FAILURE;
  }
  return UploadResult::TRANSIENT_FAILURE;
}

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.printf("Wake cause: %d\n", esp_sleep_get_wakeup_cause());

  if (!connectWifi()) {
    deepSleepFor(RETRY_SLEEP_SECONDS);
  }

  struct tm localTime;
  if (!synchronizeClock(localTime)) {
    deepSleepFor(RETRY_SLEEP_SECONDS);
  }

  if (!initializeCamera()) {
    deepSleepFor(RETRY_SLEEP_SECONDS);
  }

  camera_fb_t *photo = capturePhoto();
  if (photo == nullptr) {
    Serial.println("Camera capture failed");
    deepSleepFor(RETRY_SLEEP_SECONDS);
  }
  Serial.printf("Captured JPEG: %u bytes\n", photo->len);

  const unsigned long delays[] = {5000, 15000, 45000};
  UploadResult uploadResult = UploadResult::TRANSIENT_FAILURE;
  for (size_t attempt = 0; attempt < 3; ++attempt) {
    uploadResult = uploadPhoto(photo, localTime);
    if (uploadResult != UploadResult::TRANSIENT_FAILURE) {
      break;
    }
    if (attempt < 2) {
      delay(delays[attempt]);
    }
  }
  esp_camera_fb_return(photo);
  esp_camera_deinit();

  if (uploadResult == UploadResult::TRANSIENT_FAILURE) {
    deepSleepFor(RETRY_SLEEP_SECONDS);
  }
  deepSleepFor(secondsUntilNextCapture());
}

void loop() {}
