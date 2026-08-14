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


def implied_probability(board_odds: float) -> float | None:
    """Market's implied win chance from odds-to-one: p = 1 / (odds + 1).

    Across a whole field these sum to >100% (the bookmaker's overround / tote
    takeout). That's expected -- for a single bet what matters is the price YOU
    get paid at, so the raw implied figure is the right comparison.
    """
    if board_odds is None or board_odds < 0:
        return None
    return 1.0 / (board_odds + 1.0)


def _move_chip(info: dict | None) -> str:
    """Night->Opening price move, shown as context beside the model number.

    Deliberately display-only: the move is not folded into the composite
    score because it can't be backtested (indiarace serves only the current
    odds page, with no historical archive of what the night price was) --
    see models/odds_movement.py."""
    if not info:
        return ""
    from models.odds_movement import movement
    m = movement(info.get("stages"))
    if not m["label"]:
        return ""
    cls = "pos" if m["direction"] == "shortening" else "neg"
    arrow = "&darr;" if m["direction"] == "shortening" else "&uarr;"
    return f' &middot; <span class="rp-{cls}">{arrow} {_esc(m["label"])}</span>'


def value_rows(entries: list[dict], odds_map: dict, cushion: float = 0.25, limit: int = 10) -> str:
    """Runners ranked by VALUE (model edge over the market price), not by raw
    win chance. The likeliest horse and the profitable horse are different
    questions -- a 50% shot at 0.5/1 loses money; a 20% shot at 6/1 makes it.
    Runners with no published price fall to the bottom, marked no-price.
    """
    scored = []
    for e in entries:
        info = odds_map.get((e.get("horse_name") or "").upper())
        p = e.get("win_probability") or 0.0
        if not info:
            scored.append({"e": e, "info": None, "edge": None, "verdict": "NO PRICE", "p": p})
            continue
        board = info["odds_decimal"]
        imp = implied_probability(board)
        v = value_verdict(p, board, cushion)
        scored.append({
            "e": e, "info": info, "edge": (p - imp) * 100 if imp else None,
            "verdict": v["verdict"], "p": p, "implied": imp,
        })
    priced = [s for s in scored if s["edge"] is not None]
    unpriced = [s for s in scored if s["edge"] is None]
    priced.sort(key=lambda s: s["edge"], reverse=True)
    ordered = (priced + unpriced)[:limit]

    tone = {"BET": "pos", "THIN": "warn", "SKIP": "neg", "NO PRICE": "mut"}
    rows = []
    for s in ordered:
        e, info = s["e"], s["info"]
        cls = tone[s["verdict"]]
        if info:
            right = (
                f'<div class="rp-pct rp-{cls}">{s["edge"]:+.0f}<span style="font-size:.6em">pp</span></div>'
                f'<div class="rp-odds">{_esc(info["odds_fraction"])} &middot; mkt {s["implied"]*100:.0f}%</div>'
            )
        else:
            right = '<div class="rp-pct rp-mut">&mdash;</div><div class="rp-odds">no price</div>'
        rows.append(
            f'<div class="rp-row">'
            f'<div class="rp-id">'
            f'<div class="rp-name">{_esc(e.get("horse_name"))} '
            f'<span class="rp-badge rp-bg-{cls}">{s["verdict"]}</span></div>'
            f'<div class="rp-meta">model {s["p"]*100:.0f}% &middot; {_esc(e.get("jockey") or "")}'
            f'{_move_chip(info)}</div>'
            f"</div>"
            f'<div class="rp-val">{right}</div>'
            f"</div>"
        )
    return f'<div class="rp-card" style="padding:6px 12px">{"".join(rows)}</div>'


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


# Measured on 503 archived India races with both model scores and settled
# starting prices (scripts/backtest.py, leak-free). These are the single most
# actionable numbers in the whole archive, which is why they are surfaced at
# the point of decision rather than left in the Backtest tab: whether the model
# and the market agree predicts the outcome far better than the model's own
# confidence does.
AGREE_WIN_RATE = 53          # model pick == market favourite
DISAGREE_MODEL_RATE = 12     # model pick, when it differs from the favourite
DISAGREE_FAV_RATE = 43       # the favourite, in those same races


def market_agreement(entries: list[dict], odds_map: dict) -> dict | None:
    """Does the model's top pick match the market's shortest price?

    Returns None when there is no usable price -- the check is meaningless
    without a market, and a fabricated 'agreement' would be worse than silence.
    """
    if not entries or not odds_map:
        return None
    priced = [(e, odds_map.get((e.get("horse_name") or "").upper())) for e in entries]
    priced = [(e, i) for e, i in priced if i and i.get("odds_decimal") is not None]
    if len(priced) < 2:
        return None
    top = entries[0]
    favourite, fav_info = min(priced, key=lambda p: p[1]["odds_decimal"])
    agrees = (favourite.get("horse_name") or "").upper() == (top.get("horse_name") or "").upper()
    return {
        "agrees": agrees,
        "pick": top.get("horse_name"),
        "favourite": favourite.get("horse_name"),
        "favourite_odds": fav_info.get("odds_fraction") or f'{fav_info["odds_decimal"]:.2f}',
    }


def market_agreement_banner(agreement: dict | None) -> str:
    """The agreement check rendered as a callout.

    Deliberately blunt when the model disagrees with the market, because that
    is the case where the archive says to stand down: the model's pick won 12%
    of those races while the favourite won 43%. Presenting that as a neutral
    'note' would be underselling a 31-point gap.
    """
    if not agreement:
        return ""
    if agreement["agrees"]:
        return (
            '<div class="rp-result won">'
            f'<b>Market agrees.</b> {_esc(agreement["pick"])} is also the shortest price. '
            f'On {AGREE_WIN_RATE}% of past races where the two agreed, this pick won.'
            "</div>"
        )
    return (
        '<div class="rp-result lost">'
        f'<b>Market disagrees.</b> The board makes '
        f'{_esc(agreement["favourite"])} favourite at {_esc(agreement["favourite_odds"])}, '
        f'not {_esc(agreement["pick"])}.<br>'
        f'Across ~500 archived races the model won only {DISAGREE_MODEL_RATE}% when it '
        f'disagreed, while the favourite won {DISAGREE_FAV_RATE}%. '
        f'Historically the board has been the one to trust here -- size down or skip.'
        "</div>"
    )
