import joblib
import threading
import pandas as pd


class FailureRiskPredictor:
    def __init__(self, model_path: str):
        obj = joblib.load(model_path)

        # Trained artifacts
        self.model = obj["model"]
        self.feature_names = obj["feature_names"]

        # Thread safety (FastAPI background tasks)
        self._lock = threading.Lock()

    def predict_risk(self, features: dict) -> float:
        """
        Returns probability of failure in the next time window.
        ML is advisory — this method must NEVER raise.
        """
        if not features:
            return 0.0

        try:
            # Ensure correct feature order + names
            row = [features.get(name, 0.0) for name in self.feature_names]

            X = pd.DataFrame(
                [row],
                columns=self.feature_names
            )

            with self._lock:
                return float(self.model.predict_proba(X)[0][1])

        except Exception:
            # Safety first — ML must never crash the gateway
            return 0.0
