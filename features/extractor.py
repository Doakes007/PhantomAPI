import asyncio
import statistics
from collections import deque
from prometheus_client import REGISTRY

# Hard cap to keep features finite & JSON-safe
MAX_LATENCY_MS = 5000.0


class FeatureExtractor:
    def __init__(
        self,
        *,
        request_total,
        request_failures,
        request_timeouts,
        request_retries,
        circuit_transitions,
        latency_histogram,
        window_size=30,
        sample_interval=1,
    ):
        self.request_total = request_total._name
        self.request_failures = request_failures._name
        self.request_timeouts = request_timeouts._name
        self.request_retries = request_retries._name
        self.circuit_transitions = circuit_transitions._name
        self.latency_histogram = latency_histogram._name

        self.window_size = window_size
        self.sample_interval = sample_interval

        self.total = deque(maxlen=window_size)
        self.failures = deque(maxlen=window_size)
        self.timeouts = deque(maxlen=window_size)
        self.retries = deque(maxlen=window_size)
        self.latency_p95 = deque(maxlen=window_size)
        self.circuit_flaps = deque(maxlen=window_size)

        self._last = {}

    async def start(self):
        while True:
            self.snapshot()
            await asyncio.sleep(self.sample_interval)

    # ---------- Prometheus helpers ----------
    def _counter_value(self, metric_name: str) -> float:
        total = 0.0
        for metric in REGISTRY.collect():
            if metric.name == metric_name:
                for sample in metric.samples:
                    total += sample.value
        return total

    def _histogram_p95(self, metric_name: str) -> float:
        buckets = []
        for metric in REGISTRY.collect():
            if metric.name == metric_name:
                for sample in metric.samples:
                    if sample.name.endswith("_bucket"):
                        buckets.append((sample.labels.get("le"), sample.value))

        if not buckets:
            return 0.0

        buckets.sort(key=lambda x: float("inf") if x[0] == "+Inf" else float(x[0]))
        total = buckets[-1][1]

        if total == 0:
            return 0.0

        threshold = total * 0.95

        for le, count in buckets:
            if count >= threshold:
                if le == "+Inf":
                    return MAX_LATENCY_MS
                return float(le)

        return 0.0

    def _delta(self, name, current):
        prev = self._last.get(name, current)
        self._last[name] = current
        return max(0.0, current - prev)

    # ---------- Snapshot ----------
    def snapshot(self):
        total = self._delta("total", self._counter_value(self.request_total))
        failures = self._delta("failures", self._counter_value(self.request_failures))
        timeouts = self._delta("timeouts", self._counter_value(self.request_timeouts))
        retries = self._delta("retries", self._counter_value(self.request_retries))
        flaps = self._delta("flaps", self._counter_value(self.circuit_transitions))

        p95 = self._histogram_p95(self.latency_histogram)

        self.total.append(total)
        self.failures.append(failures)
        self.timeouts.append(timeouts)
        self.retries.append(retries)
        self.latency_p95.append(p95)
        self.circuit_flaps.append(flaps)

    # ---------- Feature computation ----------
    def compute_features(self) -> dict:
        if sum(self.total) == 0:
            return {}

        failure_ratio = sum(self.failures) / sum(self.total)
        timeout_rate = sum(self.timeouts) / sum(self.total)
        retry_rate = sum(self.retries) / sum(self.total)

        return {
            "failure_ratio": round(failure_ratio, 4),
            "failure_ratio_slope": round(self._slope(self.failures), 4),
            "p95_latency": round(self.latency_p95[-1], 2) if self.latency_p95 else 0.0,
            "latency_slope": round(self._slope(self.latency_p95), 2),
            "retry_rate": round(retry_rate, 4),
            "timeout_rate": round(timeout_rate, 4),
            "error_burstiness": round(self._burstiness(self.failures), 2),
            "circuit_flap_rate": round(sum(self.circuit_flaps) / self.window_size, 4),
        }

    def _slope(self, values):
        if len(values) < 2:
            return 0.0
        v0, v1 = values[0], values[-1]
        if not isinstance(v0, (int, float)) or not isinstance(v1, (int, float)):
            return 0.0
        return (v1 - v0) / len(values)

    def _burstiness(self, values):
        if len(values) < 2:
            return 0.0
        mean = statistics.mean(values)
        if mean == 0:
            return 0.0
        return statistics.stdev(values) / mean
