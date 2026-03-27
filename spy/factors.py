from .models import VixData


def interpret_vix(vix: float) -> VixData:
    if vix < 15:  return VixData(vix, f"{vix:.1f} CALM",     "#00d97e", True,  1.00, "Low fear — trend trades work best")
    if vix < 20:  return VixData(vix, f"{vix:.1f} NORMAL",   "#3b82f6", True,  1.00, "Normal conditions — standard sizing")
    if vix < 25:  return VixData(vix, f"{vix:.1f} ELEVATED", "#f59e0b", True,  0.75, "Elevated — reduce size 25%")
    if vix < 30:  return VixData(vix, f"{vix:.1f} HIGH",     "#f97316", False, 0.50, "High VIX — reduce size 50%, wider stops")
    return              VixData(vix, f"{vix:.1f} DANGER",    "#ef4444", False, 0.00, "Extreme fear — avoid new longs")
