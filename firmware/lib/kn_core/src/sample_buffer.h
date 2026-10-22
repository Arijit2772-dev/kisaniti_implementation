// Bounded RAM buffer of samples waiting for delivery (contract section 2).
// Samples leave the buffer only when acknowledged. On overflow the oldest is
// dropped and counted. RAM only: a power cut or reset loses the contents.
#pragma once
#include <stdint.h>
#include <string.h>
#include "sht3x_codec.h"

namespace kn {

enum class TimeQuality : uint8_t { NTP_SYNCED = 0, UPTIME_BACKFILLED, UNSYNCED };

inline const char *time_quality_name(TimeQuality q) {
  switch (q) {
    case TimeQuality::NTP_SYNCED: return "NTP_SYNCED";
    case TimeQuality::UPTIME_BACKFILLED: return "UPTIME_BACKFILLED";
    case TimeQuality::UNSYNCED: return "UNSYNCED";
  }
  return "UNSYNCED";
}

enum class SendState : uint8_t { PENDING = 0, AWAIT_ACK };

struct Sample {
  uint32_t seq;
  uint64_t uptime_ms;
  int64_t epoch_ms;  // 0 when unknown (UNSYNCED)
  TimeQuality time_quality;
  ReadStatus status;
  float temperature_c;
  float humidity_pct;
  SendState state;
  uint64_t sent_at_ms;  // uptime when last published
  uint8_t send_count;
};

template <size_t N>
class SampleBuffer {
 public:
  size_t size() const { return count_; }
  bool empty() const { return count_ == 0; }
  bool full() const { return count_ == N; }
  uint32_t dropped() const { return dropped_; }
  Sample &at(size_t i) { return items_[i]; }
  const Sample &at(size_t i) const { return items_[i]; }

  // Adds a sample; drops the oldest if full. Returns true if one was dropped.
  bool push(const Sample &s) {
    bool dropped = false;
    if (count_ == N) {
      remove_index(0);
      dropped_++;
      dropped = true;
    }
    items_[count_++] = s;
    return dropped;
  }

  // Removes the sample with this sequence number (acks may arrive out of order).
  bool remove_seq(uint32_t seq) {
    for (size_t i = 0; i < count_; i++) {
      if (items_[i].seq == seq) {
        remove_index(i);
        return true;
      }
    }
    return false;
  }

  // After the first NTP sync: give earlier samples of this boot a time
  // computed from the uptime difference. Returns how many were filled.
  size_t backfill_time(int64_t now_epoch_ms, uint64_t now_uptime_ms) {
    size_t n = 0;
    for (size_t i = 0; i < count_; i++) {
      Sample &s = items_[i];
      if (s.time_quality == TimeQuality::UNSYNCED && s.uptime_ms <= now_uptime_ms) {
        s.epoch_ms = now_epoch_ms - (int64_t)(now_uptime_ms - s.uptime_ms);
        s.time_quality = TimeQuality::UPTIME_BACKFILLED;
        n++;
      }
    }
    return n;
  }

  // Samples published but not acknowledged within timeout go back to PENDING.
  size_t expire_waiting(uint64_t now_uptime_ms, uint64_t timeout_ms) {
    size_t n = 0;
    for (size_t i = 0; i < count_; i++) {
      Sample &s = items_[i];
      if (s.state == SendState::AWAIT_ACK && now_uptime_ms - s.sent_at_ms >= timeout_ms) {
        s.state = SendState::PENDING;
        n++;
      }
    }
    return n;
  }

 private:
  void remove_index(size_t i) {
    memmove(&items_[i], &items_[i + 1], (count_ - i - 1) * sizeof(Sample));
    count_--;
  }
  Sample items_[N];
  size_t count_ = 0;
  uint32_t dropped_ = 0;
};

}  // namespace kn
