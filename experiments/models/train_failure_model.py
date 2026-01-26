import pandas as pd
import joblib
from sklearn.model_selection import train_test_split
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import classification_report, roc_auc_score

# 1. Load dataset
df = pd.read_csv("dataset/phase3_features.csv")

# 2. Drop timestamp (not predictive)
df = df.drop(columns=["timestamp"])

# 3. Split features & label
X = df.drop(columns=["label_failure_next_30s"])
y = df["label_failure_next_30s"]

# Save feature order (CRITICAL)
feature_names = list(X.columns)

# 4. Train-test split
X_train, X_test, y_train, y_test = train_test_split(
    X,
    y,
    test_size=0.3,
    random_state=42,
    stratify=y
)

# 5. Train model
model = LogisticRegression(max_iter=1000)
model.fit(X_train, y_train)

# 6. Evaluate
y_prob = model.predict_proba(X_test)[:, 1]
print(classification_report(y_test, model.predict(X_test)))
print("ROC AUC:", roc_auc_score(y_test, y_prob))

# 7. Save model artifact
joblib.dump(
    {
        "model": model,
        "feature_names": feature_names,
    },
    "experiments/models/failure_risk_model.joblib"
)
