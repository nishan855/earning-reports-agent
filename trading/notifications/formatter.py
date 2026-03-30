from ..models import Signal


def format_telegram(signal: Signal) -> str:
    if signal.direction == "WAIT":
        return ""

    icon = "🟢" if signal.direction == "LONG" else "🔴"
    conf = "⭐⭐⭐" if signal.confidence == "HIGH" else "⭐⭐" if signal.confidence == "MEDIUM" else "⭐"

    opt = ""
    if signal.option_type and signal.strike > 0:
        opt = f"\n🎯 *OPTIONS*\nBuy {signal.asset} ${signal.strike:.0f} {signal.option_type}\nExpiry: {signal.expiry_date} ({signal.dte} DTE) | Size: {signal.size}"
        if signal.est_premium_lo > 0:
            opt += f"\nEst: ${signal.est_premium_lo:.2f}–${signal.est_premium_hi:.2f}"
        if signal.breakeven > 0:
            opt += f"  B/E: ~${signal.breakeven:.2f}"

    tp2 = f"\nTP2:    ${signal.tp2:.2f}" if signal.tp2 > 0 else ""
    warn = f"\n⚠️ {signal.warnings}" if signal.warnings else ""

    return (
        f"{icon} *{signal.asset} {signal.direction}* — {signal.confidence} {conf}\n"
        f"_{signal.fired_at} ET | {signal.session}_\n\n"
        f"📋 *SETUP*\n{signal.pattern.replace('_', ' ')}\n"
        f"Level: {signal.level_name} ${signal.level_price:.2f}\n"
        f"\"{signal.narrative}\""
        f"{opt}\n\n"
        f"📊 *TRADE LEVELS*\n"
        f"Entry:  ${signal.entry:.2f}\nStop:   ${signal.stop:.2f}\n"
        f"TP1:    ${signal.tp1:.2f}{tp2}\nRR:     {signal.rr:.1f}:1\n\n"
        f"🔴 Exit if: {signal.invalidation}\n"
        f"🕐 Close all by 3:30 PM ET{warn}"
    )


def format_daily_summary(signals: list[Signal], date_str: str) -> str:
    ls = [s for s in signals if s.direction in ("LONG", "SHORT")]
    if not ls:
        return f"📊 *DAILY SUMMARY — {date_str}*\nNo signals fired today."
    lines = [f"📊 *DAILY SUMMARY — {date_str}*", f"Signals: {len(ls)}", ""]
    for s in ls:
        icon = "🟢" if s.direction == "LONG" else "🔴"
        lines.append(f"{icon} {s.asset} {s.direction} {s.fired_at} — {s.pattern.replace('_', ' ')}")
    return "\n".join(lines)


def format_premarket_brief(assets_summary: list[dict], vix: float, date_str: str) -> str:
    lines = [f"🌅 *PRE-MARKET BRIEF — {date_str}*", f"VIX: {vix:.1f}", "", "*KEY LEVELS:*"]
    for a in assets_summary[:4]:
        lines.append(f"{a['asset']}: PDH ${a['pdh']:.2f} | PDL ${a['pdl']:.2f} | Gap {a['gap']:+.1f}%")
    lines += ["", "Signals start after 10:00 AM ET"]
    return "\n".join(lines)
