"""Mobile-first design tokens + CSS for the race predictor.

Reference lock (see README "Design direction"):
- PRIMARY: Fey (feyapp.com) -- "deep-space observatory control panel". Near-black
  canvas, elevated cards, high-contrast data readouts, restrained chromatic accents.
  Chosen because Fey is a financial *research* tool, the closest product analogue to
  a race-day decision aid: calm and precise rather than a flashy gambling app.
- BORROWED (OpenSea): monospace numerals for data readouts. Role-locked to numbers.
- BORROWED (UGLYCASH leaderboard screen): two-column row template, left identity
  block / right-aligned value, plus chips as segmented filter.

TOKEN ROLE RULES -- do not repurpose:
  --act  (Cosmic Blue #479ffa) interactive/active states ONLY, never decorative
  --warn (Solar Flare #ffa16c) key value highlight ONLY
  --pos  (Emerald  #4ebe96)    positive indicators ONLY (win/placed/BET)
  --neg  (Red      #e24756)    negative/stop indicators ONLY (SKIP/loss)
  --mono (JetBrains Mono)      numeric data ONLY, never prose
Fey rule preserved: never lighten text for emphasis -- use the fixed
--text / --text-2 / --text-3 ladder.
"""
import streamlit as st

TOKENS = {
    "ink": "#0b0b0b",
    "surface": "#131313",
    "surface_2": "#191919",
    "line": "#26272d",
    "text": "#ffffff",
    "text_2": "#868f97",
    "text_3": "#5c646b",
    "pos": "#4ebe96",
    "warn": "#ffa16c",
    "act": "#479ffa",
    "neg": "#e24756",
}

_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;700&display=swap');

:root{
  --ink:#0b0b0b; --surface:#131313; --surface-2:#191919; --line:#26272d;
  --text:#fff; --text-2:#868f97; --text-3:#5c646b;
  --pos:#4ebe96; --warn:#ffa16c; --act:#479ffa; --neg:#e24756;
  --r-card:16px; --r-pill:99px; --r-input:6px;
  --font:'Inter',-apple-system,BlinkMacSystemFont,sans-serif;
  --mono:'JetBrains Mono',ui-monospace,monospace;
}

html,body,[class*="css"]{font-family:var(--font);}
.stApp{background:var(--ink);}

/* Mobile-first: reclaim Streamlit's desktop padding on small screens */
.block-container{padding:1rem 1rem 4rem!important;max-width:840px;}
@media(min-width:768px){.block-container{padding:2rem 2rem 4rem!important;}}

/* Tabs: Streamlit overflows them off-screen on phones. Make the strip a
   proper horizontal scroller with momentum and no clipped final tab. */
[data-baseweb="tab-list"]{
  overflow-x:auto!important; overflow-y:hidden!important; flex-wrap:nowrap!important;
  -webkit-overflow-scrolling:touch; scrollbar-width:none;
  gap:2px; padding-bottom:2px; border-bottom:1px solid var(--line);
}
[data-baseweb="tab-list"]::-webkit-scrollbar{display:none;}
[data-baseweb="tab"]{
  white-space:nowrap!important; flex:0 0 auto!important;
  min-height:44px; padding:0 14px!important; font-size:.85rem; font-weight:500;
  color:var(--text-2);
}
[data-baseweb="tab"][aria-selected="true"]{color:var(--text);}
[data-baseweb="tab-highlight"]{background:var(--act)!important;}

/* Touch targets: 44px minimum (Apple HIG / WCAG target size) */
.stButton>button{
  min-height:44px; border-radius:var(--r-pill); font-weight:600; font-size:.9rem;
  background:var(--surface-2); color:var(--text); border:1px solid var(--line);
  transition:background .12s ease;
}
.stButton>button:hover{background:#22242a; color:var(--text); border-color:var(--line);}
.stButton>button:focus-visible{outline:2px solid var(--act); outline-offset:2px;}

/* Race chips: st.radio(horizontal) restyled to the leaderboard-chip pattern */
div[role="radiogroup"]{gap:8px!important; flex-wrap:wrap;}
div[role="radiogroup"] label{
  background:var(--surface); border:1px solid var(--line); border-radius:var(--r-pill);
  padding:9px 15px!important; margin:0!important; min-height:44px;
  display:inline-flex; align-items:center; cursor:pointer;
  transition:border-color .12s ease, background .12s ease;
}
div[role="radiogroup"] label:hover{border-color:var(--text-3);}
div[role="radiogroup"] label>div:first-child{display:none;}  /* hide radio dot */
div[role="radiogroup"] label p{
  font-family:var(--mono); font-size:.82rem; font-weight:500; color:var(--text-2); margin:0;
}
div[role="radiogroup"] label:has(input:checked){background:var(--text); border-color:var(--text);}
div[role="radiogroup"] label:has(input:checked) p{color:var(--ink); font-weight:700;}

/* Inputs */
.stNumberInput input,.stTextInput input,.stDateInput input{
  background:var(--surface)!important; color:var(--text)!important;
  border-radius:var(--r-input)!important; border:1px solid var(--line)!important;
  font-family:var(--mono)!important; min-height:44px;
}
.stNumberInput input:focus,.stTextInput input:focus{border-color:var(--act)!important;}

/* Cards */
.rp-card{
  background:var(--surface); border:1px solid var(--line); border-radius:var(--r-card);
  padding:16px; margin-bottom:12px;
}

/* Verdict card -- the single answer for a race. Left accent bar carries the
   state colour so the verdict reads at a glance without hunting for text. */
.rp-verdict{
  background:var(--surface); border:1px solid var(--line); border-radius:var(--r-card);
  padding:18px 18px 18px 20px; margin-bottom:14px; position:relative; overflow:hidden;
}
.rp-verdict::before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--text-3);}
.rp-verdict.is-bet::before{background:var(--pos);}
.rp-verdict.is-skip::before{background:var(--neg);}
.rp-verdict-tag{
  display:inline-block; font-size:.68rem; font-weight:700; letter-spacing:.09em;
  text-transform:uppercase; padding:4px 11px; border-radius:var(--r-pill); margin-bottom:11px;
}
.rp-verdict.is-bet .rp-verdict-tag{background:rgba(78,190,150,.14); color:var(--pos);}
.rp-verdict.is-skip .rp-verdict-tag{background:rgba(226,71,86,.14); color:var(--neg);}
.rp-verdict-horse{font-size:1.5rem; font-weight:700; color:var(--text); line-height:1.15; margin:0 0 3px;}
.rp-verdict-sub{font-size:.85rem; color:var(--text-2); margin:0;}
.rp-verdict-sub b{font-family:var(--mono); color:var(--text); font-weight:500;}

/* Stat strip inside verdict */
.rp-stats{display:flex; gap:10px; margin-top:14px;}
.rp-stat{flex:1; background:var(--surface-2); border-radius:10px; padding:10px 12px; min-width:0;}
.rp-stat-k{font-size:.62rem; letter-spacing:.07em; text-transform:uppercase; color:var(--text-3); margin-bottom:3px;}
.rp-stat-v{font-family:var(--mono); font-size:1.05rem; font-weight:700; color:var(--text); line-height:1.1;}
.rp-stat-v.hl{color:var(--warn);}   /* key value highlight -- Solar Flare role */

/* Runner rows -- UGLYCASH two-column template:
   left identity block (rank + horse + connections), right-aligned numeric value */
.rp-row{
  display:flex; align-items:center; gap:12px; padding:11px 4px;
  border-bottom:1px solid var(--line);
}
.rp-row:last-child{border-bottom:none;}
.rp-rank{
  flex:0 0 26px; height:26px; border-radius:50%; background:var(--surface-2);
  display:flex; align-items:center; justify-content:center;
  font-family:var(--mono); font-size:.72rem; font-weight:700; color:var(--text-2);
}
.rp-row.is-top .rp-rank{background:var(--text); color:var(--ink);}
.rp-id{flex:1 1 auto; min-width:0;}
.rp-name{
  font-size:.94rem; font-weight:600; color:var(--text); line-height:1.25;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
}
.rp-meta{
  font-size:.72rem; color:var(--text-3); margin-top:2px;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
}
.rp-val{flex:0 0 auto; text-align:right;}
.rp-pct{font-family:var(--mono); font-size:1rem; font-weight:700; color:var(--text); line-height:1.1;}
.rp-odds{font-family:var(--mono); font-size:.68rem; color:var(--text-3); margin-top:2px;}
.rp-flag{font-size:.78rem; margin-left:5px;}

/* Value badges + tones. Colour roles preserved: green=positive/BET,
   orange=key-value/THIN, red=stop/SKIP, grey=no data. */
.rp-badge{
  display:inline-block; font-size:.58rem; font-weight:700; letter-spacing:.06em;
  padding:2px 7px; border-radius:var(--r-pill); vertical-align:middle; margin-left:5px;
}
.rp-bg-pos{background:rgba(78,190,150,.16); color:var(--pos);}
.rp-bg-warn{background:rgba(255,161,108,.16); color:var(--warn);}
.rp-bg-neg{background:rgba(226,71,86,.15); color:var(--neg);}
.rp-bg-mut{background:var(--surface-2); color:var(--text-3);}
.rp-pos{color:var(--pos);} .rp-warn{color:var(--warn);}
.rp-neg{color:var(--neg);} .rp-mut{color:var(--text-3);}

/* Section label */
.rp-label{
  font-size:.66rem; font-weight:600; letter-spacing:.09em; text-transform:uppercase;
  color:var(--text-3); margin:20px 0 8px;
}

/* Result banner */
.rp-result{
  border-radius:10px; padding:11px 13px; font-size:.85rem; margin-top:12px;
  background:var(--surface-2); color:var(--text-2); line-height:1.45;
}
.rp-result b{color:var(--text);}
.rp-result.won{background:rgba(78,190,150,.1); color:var(--pos);}
.rp-result.won b{color:var(--pos);}
.rp-result.lost{background:rgba(226,71,86,.09); color:var(--text-2);}

/* Numeric discipline: dataframes/metrics read as data, not prose */
[data-testid="stMetricValue"]{font-family:var(--mono)!important; font-size:1.3rem!important;}
[data-testid="stMetricLabel"]{color:var(--text-3)!important;}
[data-testid="stDataFrame"]{font-family:var(--mono); font-size:.8rem;}

/* Expander + alerts to match the surface system */
[data-testid="stExpander"]{
  background:var(--surface); border:1px solid var(--line)!important; border-radius:var(--r-card);
}
[data-testid="stExpander"] summary{min-height:44px; font-size:.88rem;}
[data-testid="stSidebar"]{background:var(--surface); border-right:1px solid var(--line);}
[data-testid="stSidebar"] .stButton>button{width:100%;}

h1{font-size:1.6rem!important; font-weight:700!important; letter-spacing:-.02em;}
h2{font-size:1.15rem!important; font-weight:600!important;}
h3{font-size:1rem!important; font-weight:600!important;}
@media(min-width:768px){h1{font-size:2rem!important;}}

/* Hide Streamlit chrome that wastes vertical space on a phone */
#MainMenu,footer,[data-testid="stDecoration"]{display:none;}
</style>
"""


def inject_theme() -> None:
    """Inject design tokens + mobile-first CSS. Safe to call on every rerun."""
    st.markdown(_CSS, unsafe_allow_html=True)
