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

# bump when setup-construction behavior changes (retest buffer, validity
# window, gates...) - when it changes, deployed models auto-retrain to stay
# aligned with the setup distribution they predict.
BUILDER_VERSION = "v5-phase2-builder"


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


def _logit(p: np.ndarray) -> np.ndarray:
    p_clip = np.clip(p, 1e-6, 1.0 - 1e-6)
    return np.log(p_clip / (1.0 - p_clip))


def build_purged_cv_splits(y: np.ndarray, timestamps=None, n_splits: int = 5,
                           purge_seconds: int = 0):
    """Build leak-free CV splits.

    When `timestamps` are provided, orders samples chronologically, splits into
    `n_splits` contiguous time blocks, and purges training samples within
    `purge_seconds` of either validation boundary so overlapping forward
    horizons never leak across train/val. Falls back to shuffled
    StratifiedKFold when timestamps are absent or a fold lacks both classes.
    """
    from sklearn.model_selection import StratifiedKFold

    n = len(y)
    if timestamps is not None and len(timestamps) == n and n >= n_splits * 20:
        ts = np.asarray(timestamps, dtype=np.int64)
        order = np.argsort(ts, kind="mergesort")
        blocks = np.array_split(order, n_splits)
        splits = []
        for val_idx in blocks:
            if len(val_idx) == 0:
                continue
            t_start = int(ts[val_idx[0]])
            t_end = int(ts[val_idx[-1]])
            # purge train rows whose horizon could overlap [t_start, t_end]
            in_val = np.zeros(n, dtype=bool)
            in_val[val_idx] = True
            keep_tr = (~in_val) & (
                (ts < t_start - purge_seconds) | (ts > t_end + purge_seconds)
            )
            tr_idx = np.where(keep_tr)[0]
            if len(tr_idx) >= 30 and len(np.unique(y[tr_idx])) == 2 and len(np.unique(y[val_idx])) == 2:
                splits.append((tr_idx, val_idx))
        if len(splits) >= 2:
            return splits, f"purged-time-{len(splits)}fold"

    k = min(n_splits, max(2, int(np.bincount(y).min()))) if len(np.unique(y)) == 2 else 2
    skf = StratifiedKFold(n_splits=k, shuffle=True, random_state=42)
    return list(skf.split(np.zeros(n), y)), f"stratified-shuffled-{k}fold"


class SetupML:
    def __init__(self, model_path: str):
        self.model_path = model_path
        self.model = None
        self.metrics = None
        self.trained_at = None
        self.env = None
        self.backend = None
        self.calibrator = None

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
                self.calibrator = blob.get("calibrator")
                return True
            except Exception:
                self.model = None
                self.trained_at = None
                self.env = None
        return False

    def predict(self, feats: dict):
        """Return calibrated P(win) in [0,1] or None when no model is available."""
        if self.model is None:
            return None
        x = np.array([feature_vector(feats)], dtype=float)
        try:
            p = float(self.model.predict_proba(x)[0, 1])
        except Exception:
            return None
        if self.calibrator is not None:
            try:
                if hasattr(self.calibrator, "predict_proba"):
                    # Platt sigmoid calibrator fit on log-odds
                    z = _logit(np.array([p], dtype=float)).reshape(-1, 1)
                    p = float(self.calibrator.predict_proba(z)[0, 1])
                else:
                    # Legacy IsotonicRegression calibrator
                    p = float(self.calibrator.predict(np.array([[p]]))[0])
                p = min(1.0, max(0.0, p))
            except Exception:
                pass
        return p

    def train(self, rows: list, labels: list, min_samples: int = 60,
              backend: str = "sklearn", timestamps=None,
              purge_seconds: int = 0,
              calibration_method: str = "sigmoid") -> dict:
        from sklearn.base import clone
        from sklearn.metrics import accuracy_score, brier_score_loss, roc_auc_score

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

        oof_prob = np.full(len(y), np.nan, dtype=float)
        try:
            n_splits = 5 if len(y) >= 200 else (3 if len(y) >= 90 else 2)
            splits, cv_scheme = build_purged_cv_splits(
                y, timestamps=timestamps, n_splits=n_splits, purge_seconds=purge_seconds)
            metrics["cv_scheme"] = cv_scheme
            aucs, accs = [], []
            for tr_idx, val_idx in splits:
                m_fold = clone(model)
                m_fold.fit(X[tr_idx], y[tr_idx])
                p_val = m_fold.predict_proba(X[val_idx])[:, 1]
                oof_prob[val_idx] = p_val
                aucs.append(roc_auc_score(y[val_idx], p_val))
                accs.append(accuracy_score(y[val_idx], (p_val >= 0.5).astype(int)))
            if aucs:
                metrics["cv_auc"] = round(float(np.mean(aucs)), 3)
                metrics["cv_accuracy"] = round(float(np.mean(accs)), 3)
        except Exception:
            pass

        self.model = model
        # Smooth probability calibration on OUT-OF-FOLD predictions (Platt
        # sigmoid in log-odds space by default — avoids isotonic step plateaus).
        self.calibrator = None
        try:
            valid_oof = ~np.isnan(oof_prob)
            if int(valid_oof.sum()) >= 120:
                p_oof = oof_prob[valid_oof]
                y_oof = y[valid_oof]
                if str(calibration_method).lower() == "isotonic":
                    from sklearn.isotonic import IsotonicRegression
                    cal = IsotonicRegression(out_of_bounds="clip")
                    cal.fit(p_oof, y_oof)
                    p_cal = cal.predict(p_oof)
                    metrics["calibration"] = "isotonic"
                else:
                    from sklearn.linear_model import LogisticRegression
                    cal = LogisticRegression(C=1.0)
                    cal.fit(_logit(p_oof).reshape(-1, 1), y_oof)
                    p_cal = cal.predict_proba(_logit(p_oof).reshape(-1, 1))[:, 1]
                    metrics["calibration"] = "sigmoid"
                self.calibrator = cal
                metrics["cv_brier"] = round(float(brier_score_loss(y_oof, p_cal)), 4)
        except Exception:
            self.calibrator = None
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
                     "backend": backend_used, "calibrator": self.calibrator,
                     "builder_version": BUILDER_VERSION}, self.model_path)
        return metrics
