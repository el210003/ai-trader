"""Small helpers shared across the app."""
import math


def jsonable(obj):
    """Recursively convert numpy / non-JSON-serializable values to plain python."""
    import numpy as np

    if obj is None or isinstance(obj, (bool, str)):
        return obj
    if isinstance(obj, (int,)):
        return int(obj)
    if isinstance(obj, (float,)):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return float(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        v = float(obj)
        return None if (math.isnan(v) or math.isinf(v)) else v
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [jsonable(v) for v in obj]
    if hasattr(obj, "item"):  # numpy scalars fallback
        return jsonable(obj.item())
    return str(obj)


def fmt_price(v) -> str:
    if v is None:
        return "-"
    return f"{v:.5f}".rstrip("0").rstrip(".") if v < 20 else f"{v:.2f}"
