"""Standalone login / signup pages for the live dashboard (serve_dashboard.py).

Self-contained HTML with an inline theme-aware palette matching the dashboard.
"""

from __future__ import annotations

from html import escape

_CSS = """
  :root {
    --page:#ffffff; --surface:#ffffff; --border:#e2e6ee; --field:#f5f7fa; --field-border:#d7dde7;
    --text:#111722; --text-2:#3f4b5e; --text-3:#6b7789;
    --accent:#0e8f8f; --accent-dark:#0b7373;
    --critical:#c8202b; --critical-soft:#c8202b12; --good:#128a52; --good-soft:#128a5212;
  }
  * { box-sizing:border-box; }
  /* Root font-size scales with the viewport, so the whole card grows on a big
     screen instead of looking tiny. Everything below is sized in rem. */
  html { font-size:clamp(18px, 1.25vw, 28px); }
  html, body { background:var(--page); }
  body {
    margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
    color:var(--text); padding:1.6rem;
    font-family:'Manrope',system-ui,-apple-system,'Segoe UI',sans-serif; -webkit-font-smoothing:antialiased;
  }
  .card {
    width:100%; max-width:38rem; background:var(--surface); border:1px solid var(--border);
    border-radius:1.1rem; padding:3.6rem 4rem; box-shadow:0 0.6rem 2.5rem rgba(17,23,34,.11);
  }
  .brand { display:flex; align-items:center; gap:0.7rem; font-weight:800; font-size:1.4rem; letter-spacing:.03em; }
  .brand .dot { width:0.85rem; height:0.85rem; border-radius:50%; background:var(--accent); flex-shrink:0; }
  h1 { font-size:2.5rem; line-height:1.15; margin:1.4rem 0 0.6rem; }
  .sub { color:var(--text-3); font-size:1.1rem; margin-bottom:2rem; }
  label { display:block; font-size:0.95rem; font-weight:600; color:var(--text-2); margin:1.3rem 0 0.55rem; }
  input {
    width:100%; background:var(--field); border:1px solid var(--field-border); border-radius:0.6rem;
    padding:1rem 1.15rem; font-size:1.25rem; color:var(--text); font-family:inherit;
  }
  input:focus-visible { outline:2px solid var(--accent); outline-offset:1px; background:#fff; }
  button {
    width:100%; margin-top:2.2rem; background:var(--accent); color:#fff; border:none; border-radius:0.6rem;
    padding:1.1rem; font-size:1.3rem; font-weight:700; cursor:pointer; font-family:inherit;
  }
  button:hover { background:var(--accent-dark); }
  .msg { border-radius:0.6rem; padding:0.8rem 1rem; font-size:0.95rem; font-weight:600; margin-bottom:0.3rem; }
  .msg.err { background:var(--critical-soft); color:var(--critical); }
  .msg.info { background:var(--good-soft); color:var(--good); }
  .switch { margin-top:1.7rem; text-align:center; font-size:1rem; color:var(--text-3); }
  .switch a { color:var(--accent); font-weight:600; text-decoration:none; }
  .switch a:hover { text-decoration:underline; }
"""

def render_login_page(*, error: str = "", info: str = "", next_url: str = "/") -> str:
    if error:
        message_html = f'<div class="msg err">{escape(error)}</div>'
    elif info:
        message_html = f'<div class="msg info">{escape(info)}</div>'
    else:
        message_html = ""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Sign in · Monitor</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{_CSS}</style>
</head>
<body>
  <form class="card" method="post" action="/login">
    <div class="brand"><span class="dot"></span>MONITOR</div>
    <h1>Sign in</h1>
    <div class="sub">Admin access to the monitoring dashboard.</div>
    {message_html}
    <input type="hidden" name="next" value="{escape(next_url, quote=True)}">
    <label for="login">Email</label>
    <input id="login" name="login" type="email" autocomplete="username" autofocus required>
    <label for="password">Password</label>
    <input id="password" name="password" type="password" autocomplete="current-password" required>
    <button type="submit">Sign in</button>
  </form>
</body>
</html>"""
