"""Environment consistency check — run with ANY python to see if it matches
the project requirements and the deployed model.

Usage:
    venv\\Scripts\\python.exe scripts\\check_env.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REQUIRED = ["pandas", "numpy", "sklearn", "joblib", "fastapi", "uvicorn",
            "yaml", "requests", "MetaTrader5"]


def main():
    import numpy as np
    print(f"interpreter : {sys.executable}")
    print(f"python      : {sys.version.split()[0]}")
    print(f"numpy       : {np.__version__}")
    try:
        import sklearn, joblib
        print(f"sklearn     : {sklearn.__version__} | joblib: {joblib.__version__}")
    except ImportError:
        pass

    missing = []
    for d in REQUIRED:
        try:
            __import__(d)
        except ImportError:
            missing.append(d)
    if missing:
        print(f"\n[FAIL] missing packages: {missing}")
        print("       -> pip install -r requirements.txt  (in THIS environment)")
        return 1
    print(f"deps        : all {len(REQUIRED)} present")

    # model loadability + training-env match
    from app.ai.ml_model import SetupML
    from app.config import load_config
    ml = SetupML(load_config()["ai"]["ml"]["model_path"])
    if not ml.load():
        print("model       : NOT LOADABLE — retrain in THIS environment "
              "(python -m app.main train)")
        return 1
    env = ml.env or {}
    if not env:
        print("model       : loaded, but predates env metadata (old file) — "
              "retrain to record the training environment")
        print("env match   : UNKNOWN")
        return 0
    match = env.get("numpy") == np.__version__
    print(f"model       : loaded | trained {ml.age_days:.1f} days ago "
          f"on numpy {env.get('numpy')} / sklearn {env.get('sklearn')}")
    if match:
        print("env match   : OK (model was trained in this environment)")
    else:
        print("[WARN] env MISMATCH: model was trained on numpy "
              f"{env.get('numpy')} but this runtime has numpy {np.__version__}.")
        print("       It may load today and break tomorrow. Retrain here: "
              "python -m app.main train")
        return 1
    print("\n[OK] environment is consistent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
