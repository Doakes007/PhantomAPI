class AdaptiveThresholdController:
    def __init__(
        self,
        base_threshold=0.7,
        min_threshold=0.4,
        max_threshold=0.9,
    ):
        self.base = base_threshold
        self.min = min_threshold
        self.max = max_threshold

    def compute_threshold(self, features: dict) -> float:
        threshold = self.base

        if not features:
            return threshold

        # High retry pressure → lower tolerance
        if features.get("retry_rate", 0) > 0.3:
            threshold -= 0.10

        # Latency rising → act earlier
        if features.get("latency_slope", 0) > 0:
            threshold -= 0.10

        # Circuit instability → defensive mode
        if features.get("circuit_flap_rate", 0) > 0:
            threshold -= 0.15

        # Very stable → be conservative
        if (
            features.get("failure_ratio", 0) == 0
            and features.get("latency_slope", 0) <= 0
        ):
            threshold += 0.10

        return max(self.min, min(self.max, round(threshold, 2)))
