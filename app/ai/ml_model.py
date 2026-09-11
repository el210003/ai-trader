"""ML win-probability model for trade setups (hist gradient boosting).

Backends: sklearn HistGradientBoosting (default, fastest at typical data
sizes) or XGBoost with optional CUDA (opt-in via ai.ml.backend: xgboost —
only wins at large row counts, e.g. 100k+ blended live outcomes).
The trained blob records the backend so predictions always match."""
import os
import platform
import time
from pathlib import Path

import joblib
import numpy as np

from .features import FEATURES, feature_vector


def _build_model(backend: str):
    """Build (model, backend_used). xgboost tries CUDA first, falls back to
    CPU automatically (the fit call is where a missing GPU surfaces)."""
    if backend == "xgboost":
        from xgboost import XGBClassifier
        params = dict(n_estimators=250, max_depth=4, learning_rate=0.06,
                      min_child_weight=10, reg_lambda=1.0, random_state=42,
                      tree_method="hist", eval_metric="logloss", n_jobs=4)
        try:
            return XGBClassifier(**params, device="cuda"), "xgboost-cuda"
        except Exception:
            return XGBClassifier(**params, device="cpu"), "xgboost-cpu"
    from sklearn.ensemble import HistGradientBoostingClassifier
    return (HistGradientBoostingClassifier(
        max_iter=250, max_depth=4, learning_rate=0.06,
        min_samples_leaf=10, l2_regularization=1.0, random_state=42), "sklearn")


class SetupML:
    def __init__(self, model_path: str):
        self.model_path = model_path
        self.model = None
        self.metrics = None
        self.trained_at = None
        self.env = None
        self.backend = None

    @property
    def loaded(self) -> bool:
        return self.model is not None

    @property
    def env_mismatch(self):
        """True when the model was trained under a different numpy than the
        current runtime — the pickle may load today and break tomorrow, or
        vice versa. Run everything from ONE environment."""
        if not self.env:
            return None   # unknown (old model file)
        return self.env.get("numpy") != np.__version__

    @property
    def age_days(self):
        """Model age in days, or None when never trained / unknown."""
        if not self.trained_at:
            return None
        return (time.time() - self.trained_at) / 86400.0

    def load(self) -> bool:
        if os.path.exists(self.model_path):
            try:
                blob = joblib.load(self.model_path)
                self.model = blob["model"]
                self.metrics = blob.get("metrics")
                self.trained_at = blob.get("trained_at")
                self.env = blob.get("env")
                return True
            except Exception:
                self.model = None
                self.trained_at = None
                self.env = None
        return False

    def predict(self, feats: dict):
        """Return P(win) in [0,1] or None when no model is available."""
        if self.model is None:
            return None
        x = np.array([feature_vector(feats)], dtype=float)
        try:
            return float(self.model.predict_proba(x)[0, 1])
        except Exception:
            return None

    def train(self, rows: list, labels: list, min_samples: int = 60,
              backend: str = "sklearn") -> dict:
        from sklearn.model_selection import cross_val_score

        if len(rows) < min_samples:
            raise RuntimeError(
                f"Not enough training samples ({len(rows)} < {min_samples}). "
                "Pull more history / symbols or lower ai.ml.min_train_samples.")

        X = np.array([feature_vector(r) for r in rows], dtype=float)
        y = np.array(labels, dtype=int)

        model, backend_used = _build_model(backend)
        try:
            model.fit(X, y)
        except Exception:
            if backend_used == "xgboost-cuda":   # no usable GPU -> CPU fallback
                model, backend_used = _build_model("xgboost-cpu")
                model.fit(X, y)
            else:
                raise
        self.backend = backend_used

        # Train a fresh model on the same data with permutation importance so
        # we have a real feature ranking (HGB has no native importances_).
        try:
            from sklearn.inspection import permutation_importance
            perm = permutation_importance(model, X, y, n_repeats=5,
                                          random_state=42, scoring="roc_auc")
            importances = perm.importances_mean
        except Exception:
            importances = None

        metrics = {"n_samples": int(len(y)), "win_rate": float(y.mean())}
        if importances is not None:
            metrics["feature_importances"] = {
                f: float(v) for f, v in zip(FEATURES, importances)
            }
        try:
            n_splits = 3 if len(y) >= 90 else 2
            auc = cross_val_score(model, X, y, cv=n_splits, scoring="roc_auc")
            acc = cross_val_score(model, X, y, cv=n_splits, scoring="accuracy")
            metrics["cv_auc"] = round(float(np.mean(auc)), 3)
            metrics["cv_accuracy"] = round(float(np.mean(acc)), 3)
        except Exception:
            pass

        self.model = model
        self.metrics = metrics
        self.trained_at = int(time.time())
        self.env = {"numpy": np.__version__,
                    "sklearn": __import__("sklearn").__version__,
                    "python": platform.python_version(),
                    "backend": backend_used}
        if backend_used.startswith("xgboost"):
            try:
                import xgboost
                self.env["xgboost"] = xgboost.__version__
            except Exception:
                pass
        Path(self.model_path).parent.mkdir(parents=True, exist_ok=True)
        joblib.dump({"model": model, "features": FEATURES, "metrics": metrics,
                     "trained_at": self.trained_at, "env": self.env,
                     "backend": backend_used}, self.model_path)
        return metrics
