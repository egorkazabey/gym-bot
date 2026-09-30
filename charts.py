"""Графики прогресса (matplotlib, без GUI)."""
from __future__ import annotations

import io

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import MaxNLocator  # noqa: E402

from db import Session  # noqa: E402
from parser import e1rm, fmt_w  # noqa: E402

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e6e5e0"
BLUE = "#2a78d6"
ORANGE = "#eb6834"


def _style(ax):
    ax.set_facecolor(SURFACE)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(MUTED)
    ax.tick_params(colors=MUTED, labelsize=9, length=0)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def _label_last(ax, x, y, text, color):
    ax.plot([x], [y], "o", ms=7, color=color, mec=SURFACE, mew=2, zorder=5)
    ax.annotate(text, (x, y), xytext=(8, 0), textcoords="offset points",
                va="center", fontsize=9, color=INK, fontweight="bold")


def progress_chart(exercise: str, sessions: list[Session]) -> bytes:
    days = [s.day for s in sessions]
    weighted = any(s.top_weight > 0 for s in sessions)

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(8, 6), dpi=150, sharex=True,
        gridspec_kw={"height_ratios": [2, 1], "hspace": 0.35},
    )
    fig.patch.set_facecolor(SURFACE)
    for ax in (ax1, ax2):
        _style(ax)

    if weighted:
        top = [s.top_weight for s in sessions]
        est = [max(e1rm(w, r) for w, r in s.sets) for s in sessions]
        ax1.plot(days, est, color=ORANGE, lw=2, marker="o", ms=4, label="Расчётный 1ПМ")
        ax1.plot(days, top, color=BLUE, lw=2, marker="o", ms=4, label="Рабочий вес (макс.)")
        _label_last(ax1, days[-1], est[-1], f"{est[-1]:.0f} кг", ORANGE)
        _label_last(ax1, days[-1], top[-1], f"{fmt_w(top[-1])} кг", BLUE)
        ax1.set_title("Вес, кг", loc="left", fontsize=10, color=INK_2)
        vol = [s.volume for s in sessions]
        ax2.bar(days, vol, color=BLUE, width=0.8 if len(days) > 20 else 1.5)
        ax2.set_title("Тоннаж за тренировку, кг", loc="left", fontsize=10, color=INK_2)
    else:
        mx = [s.max_reps for s in sessions]
        ax1.plot(days, mx, color=BLUE, lw=2, marker="o", ms=4)
        _label_last(ax1, days[-1], mx[-1], f"{mx[-1]}", BLUE)
        ax1.set_title("Лучший подход, повторов", loc="left", fontsize=10, color=INK_2)
        tot = [s.total_reps for s in sessions]
        ax2.bar(days, tot, color=BLUE, width=0.8 if len(days) > 20 else 1.5)
        ax2.set_title("Всего повторов за тренировку", loc="left", fontsize=10, color=INK_2)
        ax1.yaxis.set_major_locator(MaxNLocator(integer=True))

    if weighted:
        ax1.legend(loc="upper left", frameon=False, fontsize=9, labelcolor=INK_2, ncol=2,
                   bbox_to_anchor=(0, 1.02))
    ax1.margins(x=0.08, y=0.15)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    ax2.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=8))

    fig.suptitle(exercise.capitalize(), x=0.125, ha="left", fontsize=14,
                 color=INK, fontweight="bold")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=SURFACE, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()
