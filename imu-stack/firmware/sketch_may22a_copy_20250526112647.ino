// ============================================================================
// T-WATCH 2020 IMU → BLE (надёжная версия)
// Данные акселерометра в окнах 128 сэмплов @100 Гц, 50% overlap,
// бинарный кадр с CRC32, BLE-фрагментация под MTU, ACK/NACK, STATE?, backpressure.
// Единицы: **mg** (милли-g). 1000 mg ≈ 1 g.
// ============================================================================

#define LILYGO_WATCH_2020_V1
#define LILYGO_WATCH_HAS_BMA423


#include <Arduino.h>
#include <stdarg.h>
#include <math.h>
#include <LilyGoWatch.h>
#include <NimBLEDevice.h>
#include <Wire.h>
#include <esp_heap_caps.h>


//======================= CONFIG =========================
#define DEVICE_NAME        "T-Watch-IMU"
#define FS_HZ              100
#define WIN_N              128
#define OVERLAP_N          (WIN_N/2)
#define SAMPLE_Q_DEPTH     1024
#define FRAME_POOL_SZ      6
#define TX_NOTIFY_DELAY_MS 8
#define DEV_ID             1

// BLE UUIDs
static const char* SVC_UUID = "6f2d5d52-0f4d-4b2a-a02b-5a7a5b3a0e11";
static const char* TX_UUID  = "f5c8b9d0-3a5d-4d9d-9d67-2d7f1b9e4b22";
static const char* RX_UUID  = "e8b6a830-8f6b-4d9c-a71c-6d8c2a3a5f33";

//================= ПРОТОКОЛ (binary) ====================
#define MAGIC_FRAME 0xB10F
#define MAGIC_FRAG  0xB1F1

struct __attribute__((packed)) FrameHdr {
  uint16_t magic;
  uint8_t  ver;
  uint8_t  msg_type;
  uint32_t dev_id;
  uint32_t seq;
  uint64_t ts0_ns;
  uint16_t fs_hz;
  uint16_t n;
  uint8_t  axes;
  uint8_t  batt;
  int8_t   rssi;
  uint8_t  parts;        // заполняется в TX
  uint32_t payload_len;  // байт данных после заголовка (без CRC)
}; // 32 bytes

struct __attribute__((packed)) FragHdr {
  uint16_t magic;
  uint8_t  ver;
  uint8_t  part_idx;
  uint32_t seq;
  uint16_t frag_len;
};

//==================== HW & BLE ==========================
TTGOClass     *watch;
TFT_eSPI      *tft;
BMA           *bma;
AXP20X_Class  *axp;

NimBLEServer*         gServer = nullptr;
NimBLECharacteristic  *gTx = nullptr, *gRx = nullptr;
volatile bool         g_connected = false;
volatile bool         g_active    = false;

uint16_t              g_mtu_payload = 182;
volatile int64_t      g_timeOffsetSec = 0;
volatile bool         g_timeSync = false;
volatile uint32_t     g_seq  = 0;

// счётчики и адаптивная задержка
volatile uint32_t g_dropSamples=0, g_dropFrames=0, g_sentFrames=0;
volatile uint32_t g_txDelayMs = TX_NOTIFY_DELAY_MS;

// Текущий выбранный диапазон BMA423 (для конвертации LSB→mg)
static uint8_t g_range_sel = BMA4_ACCEL_RANGE_4G;

//==================== CRC32 ============================
static uint32_t crc32_le(const uint8_t* d, size_t n) {
  uint32_t c = 0xFFFFFFFFu;
  for (size_t i=0;i<n;i++) {
    c ^= d[i];
    for (int k=0;k<8;k++) c = (c>>1) ^ (0xEDB88320u & (-(int)(c & 1)));
  }
  return ~c;
}

//================== TIME HELPERS =======================
static inline uint64_t hub_time_ns() {
  uint64_t sec  = (uint64_t)(millis()/1000UL) + (uint64_t)g_timeOffsetSec;
  uint64_t frac = (uint64_t)(millis()%1000UL) * 1000000ULL;
  return sec*1000000000ULL + frac;
}

//================== UNIT CONVERSION (LSB -> mg) =========
static inline float lsb_per_g_for(uint8_t range_sel) {
  switch (range_sel) {
    case BMA4_ACCEL_RANGE_2G:  return 1024.0f;
    case BMA4_ACCEL_RANGE_4G:  return 512.0f;
    case BMA4_ACCEL_RANGE_8G:  return 256.0f;
    case BMA4_ACCEL_RANGE_16G: return 128.0f;
    default:                   return 512.0f; // безопасный дефолт как 4g
  }
}
static inline int16_t lsb_to_mg(int16_t v_lsb) {
  float mg = (v_lsb / lsb_per_g_for(g_range_sel)) * 1000.0f;
  long v = lroundf(mg);
  if (v >  32767) v =  32767;
  if (v < -32768) v = -32768;
  return (int16_t)v;
}

//==================== QUEUES / POOL =====================
struct Sample { int16_t ax, ay, az; uint64_t ts_ns; }; // ax/ay/az — В MG

QueueHandle_t qSamples = nullptr;
QueueHandle_t qFrames  = nullptr;

static const uint32_t FRAME_BYTES = sizeof(FrameHdr) + (WIN_N*3*2) + 4;

struct FrameBuf {
  uint8_t* data;
  uint32_t len;
  bool     inUse;
};

SemaphoreHandle_t g_poolMux = nullptr;
FrameBuf g_pool[FRAME_POOL_SZ];

// Прототипы
static FrameBuf* pool_acquire();
static void      pool_release(FrameBuf* fb);
static inline void tx_fragmented(FrameBuf* fb);

static FrameBuf* pool_acquire() {
  xSemaphoreTake(g_poolMux, portMAX_DELAY);
  for (int i=0;i<FRAME_POOL_SZ;i++) {
    if (!g_pool[i].inUse) {
      g_pool[i].inUse = true;
      xSemaphoreGive(g_poolMux);
      return &g_pool[i];
    }
  }
  xSemaphoreGive(g_poolMux);
  return nullptr;
}
static void pool_release(FrameBuf* fb) {
  if (!fb) return;
  xSemaphoreTake(g_poolMux, portMAX_DELAY);
  fb->inUse = false;
  xSemaphoreGive(g_poolMux);
}

//==================== BLE CALLBACKS =====================
class ServerCB : public NimBLEServerCallbacks {
  static void _onConnect() {
    g_connected = true;
    Serial.println("[BLE] onConnect()");
  }
  static void _onDisconnect(NimBLEServer* s) {
    g_connected   = false;
    g_active      = false;
    g_timeSync    = false;
    g_seq         = 0;
    g_dropSamples = g_dropFrames = g_sentFrames = 0;
    if (qSamples) xQueueReset(qSamples);
    if (qFrames)  xQueueReset(qFrames);
    if (g_poolMux) {
      xSemaphoreTake(g_poolMux, portMAX_DELAY);
      for (int i=0;i<FRAME_POOL_SZ;i++) g_pool[i].inUse = false;
      xSemaphoreGive(g_poolMux);
    }
    Serial.println("[BLE] onDisconnect() → re-adv");
    if (s) s->startAdvertising();
  }
  static void _onMTU(uint16_t MTU) {
    g_mtu_payload = (MTU > 3) ? (MTU - 3) : 20;
    if (g_mtu_payload > 200) g_mtu_payload = 200;
    Serial.printf("[BLE] MTU=%u (payload=%u)\n", MTU, g_mtu_payload);
  }
public:
  void onConnect(NimBLEServer* s) { _onConnect(); }
  void onConnect(NimBLEServer* s, ble_gap_conn_desc*) { _onConnect(); }
  void onConnect(NimBLEServer* s, NimBLEConnInfo&) { _onConnect(); }
  void onDisconnect(NimBLEServer* s) { _onDisconnect(s); }
  void onDisconnect(NimBLEServer* s, ble_gap_conn_desc*) { _onDisconnect(s); }
  void onDisconnect(NimBLEServer* s, NimBLEConnInfo&) { _onDisconnect(s); }
  void onMTUChange(uint16_t MTU, ble_gap_conn_desc*) { _onMTU(MTU); }
  void onMTUChange(uint16_t MTU, NimBLEConnInfo&) { _onMTU(MTU); }
};

class TxCB : public NimBLECharacteristicCallbacks {
  static void _hello(NimBLECharacteristic* c){
    Serial.println("[BLE] TX subscribed");
    const char* hello = "HELLO";
    c->setValue((uint8_t*)hello, 5);
    c->notify();
  }
public:
  void onSubscribe(NimBLECharacteristic* c, uint16_t) { _hello(c); }
  void onSubscribe(NimBLECharacteristic* c, ble_gap_conn_desc*, uint16_t) { _hello(c); }
  void onSubscribe(NimBLECharacteristic* c, NimBLEConnInfo&, uint16_t) { _hello(c); }
};

// ---------- RX: ACK/NACK, STATE?, TIME, START/STOP ----------
class RxCB : public NimBLECharacteristicCallbacks {
  void handleWrite(NimBLECharacteristic* c) {
    std::string v = c->getValue();
    trimInPlace(v);

    Serial.print("[BLE] RX got: ");
    Serial.println(v.c_str());

    if (startsWith(v, "TIME:")) {
      const char* p = v.c_str() + 5;
      long hub_unix = atol(p);
      long dev_sec  = millis()/1000UL;
      g_timeOffsetSec = (int64_t)hub_unix - (int64_t)dev_sec;
      g_timeSync = true;
      Serial.printf("[TIME] sync ok, offset(s)=%ld\n", (long)g_timeOffsetSec);
      sendTX("ACK:TIME");
      sendState();
      return;
    }
    if (startsWith(v, "TIME_PING:")) {
      char out[48];
      long t1 = millis()/1000UL;
      snprintf(out, sizeof(out), "TIME_PONG:%ld", t1);
      sendTX(out);
      Serial.println("[TIME] pong sent");
      return;
    }
    if (v == "PING")    { sendTX("PONG"); return; }
    if (v == "STATE?")  { sendState(); return; }
    if (v == "START") {
      if (g_timeSync) { g_active = true; Serial.println("[RUN] active=TRUE"); sendTX("ACK:START"); }
      else            { Serial.println("[RUN] START ignored (no time sync)"); sendTX("NACK:START_NO_TIME"); }
      sendState(); return;
    }
    if (v == "STOP")  { g_active = false; Serial.println("[RUN] active=FALSE"); sendTX("ACK:STOP"); sendState(); return; }

    char msg[96];
    snprintf(msg, sizeof(msg), "NACK:UNKNOWN_CMD:%s", v.c_str());
    sendTX(msg);
  }
public:
  void onWrite(NimBLECharacteristic* c) { handleWrite(c); }
  void onWrite(NimBLECharacteristic* c, ble_gap_conn_desc*) { handleWrite(c); }
  void onWrite(NimBLECharacteristic* c, NimBLEConnInfo&) { handleWrite(c); }

private:
  static void sendTX(const char* s) {
    if (!gTx || !s) return;
    gTx->setValue((uint8_t*)s, (uint16_t)strlen(s));
    gTx->notify();
  }
  static void sendTXf(const char* fmt, ...) {
    if (!gTx || !fmt) return;
    char buf[128];
    va_list ap; va_start(ap, fmt); vsnprintf(buf, sizeof(buf), fmt, ap); va_end(ap);
    gTx->setValue((uint8_t*)buf, (uint16_t)strlen(buf));
    gTx->notify();
  }
  static bool startsWith(const std::string& s, const char* pref) {
    if (!pref) return false; size_t n = strlen(pref);
    return s.size() >= n && memcmp(s.data(), pref, n) == 0;
  }
  static void trimInPlace(std::string& s) {
    auto issp = [](unsigned char c){ return c==' '||c=='\t'||c=='\r'||c=='\n'; };
    size_t i=0, j=s.size();
    while (i<j && issp((unsigned char)s[i])) ++i;
    while (j>i && issp((unsigned char)s[j-1])) --j;
    if (i!=0 || j!=s.size()) s.assign(s.data()+i, j-i);
  }
  static void sendState() {
    const int sync   = g_timeSync ? 1 : 0;
    const int active = g_active   ? 1 : 0;
    const unsigned mtu = g_mtu_payload;
    int batt = -1;
    if (axp) { int b = axp->getBattPercentage(); if (b >= 0 && b <= 100) batt = b; }
    if (batt >= 0) sendTXf("STATE:SYNC=%d,ACTIVE=%d,MTU=%u,BATT=%d,DROP_S=%lu,DROP_F=%lu,SENT=%lu",
                           sync, active, mtu, batt, g_dropSamples, g_dropFrames, g_sentFrames);
    else           sendTXf("STATE:SYNC=%d,ACTIVE=%d,MTU=%u,DROP_S=%lu,DROP_F=%lu,SENT=%lu",
                           sync, active, mtu, g_dropSamples, g_dropFrames, g_sentFrames);
  }
};

//==================== IMU TASK ==========================
static void imuTask(void*){
  TickType_t last = xTaskGetTickCount();
  const TickType_t period = pdMS_TO_TICKS(1000/FS_HZ);

  // прогрев датчика
  for (int i=0;i<10;i++){ Accel d; bma->getAccel(d); vTaskDelay(pdMS_TO_TICKS(20)); }

  while (true) {
    vTaskDelayUntil(&last, period);
    if (!g_active) continue;

    Accel a; if (!bma->getAccel(a)) continue;
    // a.x/y/z — это **LSB** (12 бит после сдвига). Конвертируем в mg.
    Sample s;
    s.ax = lsb_to_mg(a.x);
    s.ay = lsb_to_mg(a.y);
    s.az = lsb_to_mg(a.z);
    s.ts_ns = hub_time_ns();

    if (xQueueSend(qSamples, &s, 0) != pdTRUE) g_dropSamples++;
  }
}

//==================== PACKAGER TASK =====================
static void packTask(void*){
  static int16_t X[WIN_N], Y[WIN_N], Z[WIN_N]; // в mg
  static int filled = 0;
  static uint64_t ts0 = 0;

  while (true) {
    Sample s;
    if (xQueueReceive(qSamples, &s, portMAX_DELAY) != pdTRUE) continue;

    if (filled == 0) ts0 = s.ts_ns;
    if (filled < WIN_N) { X[filled]=s.ax; Y[filled]=s.ay; Z[filled]=s.az; filled++; }

    if (filled >= WIN_N) {
      FrameBuf* fb = pool_acquire();
      if (!fb) {
        memmove(X, X+(WIN_N-OVERLAP_N), OVERLAP_N * sizeof(int16_t));
        memmove(Y, Y+(WIN_N-OVERLAP_N), OVERLAP_N * sizeof(int16_t));
        memmove(Z, Z+(WIN_N-OVERLAP_N), OVERLAP_N * sizeof(int16_t));
        filled = OVERLAP_N;
        ts0 += (uint64_t)((WIN_N-OVERLAP_N)*(1000000000ULL/FS_HZ));
        continue;
      }

      uint8_t* frame = fb->data;
      FrameHdr* h = (FrameHdr*)frame;
      h->magic = MAGIC_FRAME; h->ver=1; h->msg_type=1;
      h->dev_id = DEV_ID; h->seq = g_seq++;
      h->ts0_ns = ts0;
      h->fs_hz = FS_HZ; h->n = WIN_N;
      h->axes = 0b111; h->batt = axp? axp->getBattPercentage(): 100;
      h->rssi = -60;    h->parts = 0;
      h->payload_len = WIN_N*3*2;

      size_t off = sizeof(FrameHdr);
      memcpy(frame+off, X, WIN_N*2); off += WIN_N*2;
      memcpy(frame+off, Y, WIN_N*2); off += WIN_N*2;
      memcpy(frame+off, Z, WIN_N*2); off += WIN_N*2;

      uint32_t crc = crc32_le(frame, sizeof(FrameHdr)+h->payload_len);
      memcpy(frame+off, &crc, 4);
      fb->len = sizeof(FrameHdr)+h->payload_len+4;

      if (xQueueSend(qFrames, &fb, 0) != pdTRUE) { g_dropFrames++; pool_release(fb); }

      memmove(X, X+(WIN_N-OVERLAP_N), OVERLAP_N*2);
      memmove(Y, Y+(WIN_N-OVERLAP_N), OVERLAP_N*2);
      memmove(Z, Z+(WIN_N-OVERLAP_N), OVERLAP_N*2);
      filled = OVERLAP_N;
      ts0 += (uint64_t)((WIN_N-OVERLAP_N)*(1000000000ULL/FS_HZ));
    }
  }
}

//==================== BLE TX (fragmentation) =============
static inline void tx_fragmented(FrameBuf* fb) {
  if (!g_connected || !gTx || !fb) return;

  uint8_t*  frame = fb->data;
  FrameHdr* H     = (FrameHdr*)frame;

  const uint32_t hdr_sz   = sizeof(FrameHdr);
  const uint32_t pay_len  = H->payload_len;
  const uint32_t crc_sz   = 4;
  const uint32_t total_sz = hdr_sz + pay_len + crc_sz;
  if (fb->len < total_sz) return;

  uint32_t remaining_tail = pay_len + crc_sz;
  uint8_t  parts = 0;
  {
    uint32_t tmp = remaining_tail;
    while (tmp > 0) {
      const uint16_t head  = sizeof(FragHdr);
      uint16_t avail = (g_mtu_payload > head) ? (g_mtu_payload - head) : 0;
      if (parts == 0) {
        if (avail < hdr_sz) return;
        avail -= hdr_sz;
      }
      const uint16_t take = (tmp > avail) ? avail : (uint16_t)tmp;
      tmp -= take;
      parts++;
    }
    if (parts == 0) parts = 1;
  }

  H->parts = parts;
  uint32_t crc = crc32_le(frame, hdr_sz + pay_len);
  memcpy(frame + hdr_sz + pay_len, &crc, crc_sz);

  Serial.printf("[FRAG] seq=%lu parts=%u total=%u mtu=%u\n", H->seq, parts, total_sz, g_mtu_payload);

  uint32_t cursor = 0;
  remaining_tail  = pay_len + crc_sz;

  for (uint8_t i = 0; i < parts; i++) {
    if (!g_connected) {
      Serial.printf("[FRAG] BLE disconnected, stopping\n");
      return;
    }

    FragHdr fh;
    fh.magic    = MAGIC_FRAG;
    fh.ver      = 1;
    fh.part_idx = i;
    fh.seq      = H->seq;

    const uint16_t head  = sizeof(FragHdr);
    uint16_t avail = (g_mtu_payload > head) ? (g_mtu_payload - head) : 0;

    uint16_t firstExtra = 0;
    if (i == 0) {
      if (avail < hdr_sz) return;
      avail -= hdr_sz;
      firstExtra = hdr_sz;
    }

    const uint16_t take = (remaining_tail > avail) ? avail : (uint16_t)remaining_tail;
    fh.frag_len = firstExtra + take;

    const uint16_t fragSize = sizeof(FragHdr) + fh.frag_len;
    if (fragSize > g_mtu_payload) {
        Serial.printf("[FRAG] ERROR: fragSize=%u > mtu=%u\n", fragSize, g_mtu_payload);
        return;
    }
    if (fragSize == 0) {
        Serial.printf("[FRAG] ERROR: fragSize=0 for part %u\n", i);
        return;
    }

    uint8_t  frag[260];
    uint16_t off = 0;
    memcpy(frag + off, &fh, sizeof(fh)); off += sizeof(fh);
    if (i == 0) { memcpy(frag + off, frame, hdr_sz); off += hdr_sz; }
    if (take)   { memcpy(frag + off, frame + hdr_sz + cursor, take); off += take; }

    Serial.printf("[FRAG] sending part %u/%u: size=%u cursor=%lu remaining=%lu\n",
                  i+1, parts, off, cursor, remaining_tail);

    gTx->setValue(frag, off);
    gTx->notify();

    if (i < parts - 1) {
      uint32_t delay_ms = g_txDelayMs;
      if (off > 150) delay_ms *= 2;
      if (off > 180) delay_ms *= 2;
      delay_ms += 5;
      Serial.printf("[FRAG] delay=%ums before next fragment\n", delay_ms);
      delay(delay_ms);
    }

    cursor         += take;
    remaining_tail -= take;
  }

  g_sentFrames++;
}

static void bleTxTask(void*){
  while (true) {
    FrameBuf* fb = nullptr;
    if (xQueueReceive(qFrames, &fb, portMAX_DELAY) == pdTRUE && fb) {
      UBaseType_t qfill = uxQueueMessagesWaiting(qFrames);
      UBaseType_t qfree = uxQueueSpacesAvailable(qFrames);
      UBaseType_t qcap  = qfill + qfree;
      if (qcap > 0) {
        float load = (float)qfill / (float)qcap; // 0..1
        g_txDelayMs = (uint32_t)(TX_NOTIFY_DELAY_MS * (1.0f + 3.0f * load));
      }

      if (qfill > 2) {
        delay(g_txDelayMs * 3);
      } else if (qfill > 1) {
        delay(g_txDelayMs * 2);
      } else {
        delay(g_txDelayMs);
      }

      tx_fragmented(fb);
      pool_release(fb);
    }
  }
}

//==================== UI ================================
static void drawStatus() {
  static uint32_t lastSent=0, lastMs=0, sentPerSec=0;
  uint32_t now = millis();
  if (now - lastMs >= 1000) {
    sentPerSec = g_sentFrames - lastSent;
    lastSent = g_sentFrames;
    lastMs = now;
  }

  tft->fillScreen(TFT_BLACK);
  tft->setTextFont(2);
  tft->setTextColor(TFT_CYAN, TFT_BLACK);
  tft->drawString("BodyNet IMU (mg)", 20, 8);
  tft->setTextColor(TFT_WHITE, TFT_BLACK);
  tft->drawString(String("BLE: ")   + (g_connected?"ON":"OFF"), 10, 40);
  tft->drawString(String("SYNC: ")  + (g_timeSync?"OK":"NO"),    10, 60);
  tft->drawString(String("ACTIVE: ")+ (g_active?"YES":"NO"),     10, 80);
  tft->drawString(String("MTU: ")   + g_mtu_payload,             10, 100);
  if (axp) tft->drawString(String("Batt: ")+axp->getBattPercentage()+"%", 10, 120);
  tft->drawString(String("Sent/s: ")+sentPerSec,                 10, 140);
  tft->drawString(String("DropS: ") + (uint32_t)g_dropSamples,   10, 160);
  tft->drawString(String("DropF: ") + (uint32_t)g_dropFrames,    10, 180);
}

//==================== SETUP =============================
void setup() {
  Serial.begin(115200);

  // Watch
  watch = TTGOClass::getWatch();
  watch->begin();
  watch->openBL();
  tft = watch->tft;
  bma = watch->bma;
  axp = watch->power;
  drawStatus();

  // BMA423 @ FS_HZ
  if (bma) {
    Acfg cfg;
    cfg.odr = (FS_HZ==50) ? BMA4_OUTPUT_DATA_RATE_50HZ : BMA4_OUTPUT_DATA_RATE_100HZ;
    cfg.range = BMA4_ACCEL_RANGE_4G;            // диапазон измерения
    cfg.bandwidth = BMA4_ACCEL_NORMAL_AVG4;
    cfg.perf_mode = BMA4_CONTINUOUS_MODE;

    bma->accelConfig(cfg);
    g_range_sel = cfg.range;                    // сохраняем для конвертации LSB→mg
    bma->enableAccel();
  }

  // Queues
  qSamples = xQueueCreate(SAMPLE_Q_DEPTH, sizeof(Sample));
  qFrames  = xQueueCreate(FRAME_POOL_SZ, sizeof(FrameBuf*));

  // Frame pool init
  g_poolMux = xSemaphoreCreateMutex();
  for (int i=0;i<FRAME_POOL_SZ;i++) {
    g_pool[i].data = (uint8_t*)heap_caps_malloc(FRAME_BYTES, MALLOC_CAP_8BIT);
    g_pool[i].len  = FRAME_BYTES;
    g_pool[i].inUse= false;
  }

  // BLE
  NimBLEDevice::init(DEVICE_NAME);
  NimBLEDevice::setDeviceName(DEVICE_NAME);
  NimBLEDevice::setPower(ESP_PWR_LVL_P6);
  NimBLEDevice::setSecurityAuth(false,false,false);
  NimBLEDevice::setSecurityIOCap(BLE_HS_IO_NO_INPUT_OUTPUT);
  NimBLEDevice::setMTU(185);

  gServer = NimBLEDevice::createServer();
  gServer->setCallbacks(new ServerCB());

  NimBLEService* svc = gServer->createService(SVC_UUID);
  gTx = svc->createCharacteristic(TX_UUID, NIMBLE_PROPERTY::NOTIFY | NIMBLE_PROPERTY::INDICATE);
  gTx->setCallbacks(new TxCB());
  gRx = svc->createCharacteristic(RX_UUID,
        NIMBLE_PROPERTY::READ | NIMBLE_PROPERTY::WRITE | NIMBLE_PROPERTY::WRITE_NR);

  gRx->setValue("READY");
  gRx->setCallbacks(new RxCB());
  svc->start();

  NimBLEAdvertising* adv = NimBLEDevice::getAdvertising();
  adv->addServiceUUID(SVC_UUID);
  adv->setMinInterval(0x20);
  adv->setMaxInterval(0x30);
  adv->start();
  Serial.println("[BLE] Advertising started");

  // Tasks
  xTaskCreatePinnedToCore(imuTask,  "imu",   4096, nullptr, 3, nullptr, 1);
  xTaskCreatePinnedToCore(packTask, "pack",  6144, nullptr, 2, nullptr, 1);
  xTaskCreatePinnedToCore(bleTxTask,"bletx", 4096, nullptr, 1, nullptr, 0);

  Serial.println("Setup complete");
}

void loop() {
  static uint32_t last=0;
  if (gServer) {
    bool nowConnected = (gServer->getConnectedCount() > 0);
    if (nowConnected != g_connected) {
      g_connected = nowConnected;
      Serial.printf("[BLE] connected=%d (via poll)\n", (int)g_connected);
      if (!g_connected) {
        g_active = false;
      }
    }
  }

  if (millis()-last > 1000) {
    drawStatus();
    last = millis();

    if (g_active && g_connected) {
      UBaseType_t qSamplesFill = qSamples ? uxQueueMessagesWaiting(qSamples) : 0;
      UBaseType_t qFramesFill = qFrames ? uxQueueMessagesWaiting(qFrames) : 0;

      if (qFramesFill > 4) {
        Serial.printf("[STATUS] Queues overloaded, pausing...\n");
        g_active = false;
        delay(1000);
        g_active = true;
      }

      Serial.printf("[STATUS] active=%d samples_q=%u frames_q=%u\n",
                    g_active, qSamplesFill, qFramesFill);
    }
  }
  delay(10);
}
