"""Mobile-first UI components for the race-day view.

These render explicit HTML instead of st.dataframe because Streamlit tables
scroll horizontally on a phone -- a 12-column runner table is unusable at
375px. The row template follows the UGLYCASH leaderboard screen: left identity
block (rank + horse + connections), right-aligned numeric value.
"""
import html


def fair_odds(probability: float) -> float | None:
    """Break-even decimal odds-to-one for a given win probability.

    fair = (1 / p) - 1. Bet only when the board pays MORE than this, because
    the tote rake (~20%) and model error both eat into the margin. Returns
    None for a zero/invalid probability.
    """
    if not probability or probability <= 0:
        return None
    return (1.0 / probability) - 1.0


def value_verdict(probability: float, board_odds: float, cushion: float = 0.25) -> dict:
    """Compare the live board price against fair odds.

    cushion = fraction of headroom demanded above break-even before calling a
    bet. 0.25 means the board must pay 25% more than fair odds -- the buffer
    that covers tote rake and the fact this model is young.
    """
    fair = fair_odds(probability)
    if fair is None:
        return {"verdict": "NO DATA", "fair": None, "required": None, "edge_pct": None}
    required = fair * (1 + cushion)
    if board_odds <= 0:
        return {"verdict": "NO DATA", "fair": fair, "required": required, "edge_pct": None}
    edge_pct = (board_odds - fair) / fair * 100.0
    if board_odds >= required:
        verdict = "BET"
    elif board_odds >= fair:
        verdict = "THIN"
    else:
        verdict = "SKIP"
    return {"verdict": verdict, "fair": fair, "required": required, "edge_pct": edge_pct}


def _esc(v) -> str:
    return html.escape(str(v)) if v is not None else ""


def verdict_card(*, state: str, tag: str, horse: str, subtitle: str, stats: list[dict]) -> str:
    """state: 'bet' | 'skip' | 'neutral'. stats: [{'k','v','hl'(bool)}]."""
    cls = {"bet": "is-bet", "skip": "is-skip"}.get(state, "")
    tiles = "".join(
        f'<div class="rp-stat"><div class="rp-stat-k">{_esc(s["k"])}</div>'
        f'<div class="rp-stat-v{" hl" if s.get("hl") else ""}">{_esc(s["v"])}</div></div>'
        for s in stats
    )
    return (
        f'<div class="rp-verdict {cls}">'
        f'<span class="rp-verdict-tag">{_esc(tag)}</span>'
        f'<p class="rp-verdict-horse">{_esc(horse)}</p>'
        f'<p class="rp-verdict-sub">{subtitle}</p>'
        f'<div class="rp-stats">{tiles}</div>'
        f"</div>"
    )


def runner_rows(entries: list[dict], limit: int = 8) -> str:
    """Ranked runner list. Left: rank + horse + jockey/trainer. Right: win% + fair odds."""
    rows = []
    for i, e in enumerate(entries[:limit]):
        prob = e.get("win_probability") or 0.0
        fair = fair_odds(prob)
        meta_bits = [b for b in (e.get("jockey"), e.get("trainer")) if b]
        flags = ""
        if e.get("workout_n"):
            score = e.get("workout_score", 0.5)
            if score >= 0.62:
                flags += '<span class="rp-flag">&#128293;</span>'
            elif score <= 0.38:
                flags += '<span class="rp-flag">&#9888;&#65039;</span>'
        if e.get("owner_sponsor_flag"):
            flags += '<span class="rp-flag">&#128279;</span>'
        odds_html = f'<div class="rp-odds">fair {fair:.1f}/1</div>' if fair else '<div class="rp-odds">&mdash;</div>'
        meta_html = " &middot; ".join(_esc(b) for b in meta_bits) or "&nbsp;"
        top_cls = " is-top" if i == 0 else ""
        rows.append(
            f'<div class="rp-row{top_cls}">'
            f'<div class="rp-rank">{i + 1}</div>'
            f'<div class="rp-id">'
            f'<div class="rp-name">{_esc(e.get("horse_name"))}{flags}</div>'
            f'<div class="rp-meta">{meta_html}</div>'
            f"</div>"
            f'<div class="rp-val">'
            f'<div class="rp-pct">{prob * 100:.0f}%</div>'
            f"{odds_html}"
            f"</div></div>"
        )
    return f'<div class="rp-card" style="padding:6px 12px">{"".join(rows)}</div>'


def section_label(text: str) -> str:
    return f'<div class="rp-label">{_esc(text)}</div>'


def result_banner(*, outcome: str, text: str) -> str:
    """outcome: 'won' | 'lost' | 'neutral'."""
    cls = {"won": "won", "lost": "lost"}.get(outcome, "")
    return f'<div class="rp-result {cls}">{text}</div>'
