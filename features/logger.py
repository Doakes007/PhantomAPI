import asyncio
import csv
import os
import time
from typing import Dict


class FeatureLogger:
    def __init__(
        self,
        *,
        extractor,
        output_path="dataset/phase3_features.csv",
        log_interval=5,
        failure_threshold=0.5,
        label_window=30,
    ):
        self.extractor = extractor
        self.output_path = output_path
        self.log_interval = log_interval
        self.failure_threshold = failure_threshold
        self.label_window = label_window

        self._buffer = []

        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        if not os.path.exists(output_path):
            with open(output_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp",
                    "failure_ratio",
                    "failure_ratio_slope",
                    "p95_latency",
                    "latency_slope",
                    "retry_rate",
                    "timeout_rate",
                    "error_burstiness",
                    "circuit_flap_rate",
                    "label_failure_next_30s",
                ])

    async def start(self):
        while True:
            self._log_once()
            await asyncio.sleep(self.log_interval)

    def _log_once(self):
        features = self.extractor.compute_features()
        if not features:
            return

        now = time.time()

        # Store temporarily to label later
        self._buffer.append((now, features))

        # Assign labels to old samples
        cutoff = now - self.label_window
        labeled = []

        for ts, feat in self._buffer:
            if ts <= cutoff:
                label = 1 if feat["failure_ratio"] >= self.failure_threshold else 0
                labeled.append((ts, feat, label))

        # Remove labeled samples from buffer
        self._buffer = [
            (ts, feat) for ts, feat in self._buffer if ts > cutoff
        ]

        # Write labeled rows
        with open(self.output_path, "a", newline="") as f:
            writer = csv.writer(f)
            for ts, feat, label in labeled:
                writer.writerow([
                    ts,
                    feat["failure_ratio"],
                    feat["failure_ratio_slope"],
                    feat["p95_latency"],
                    feat["latency_slope"],
                    feat["retry_rate"],
                    feat["timeout_rate"],
                    feat["error_burstiness"],
                    feat["circuit_flap_rate"],
                    label,
                ])
