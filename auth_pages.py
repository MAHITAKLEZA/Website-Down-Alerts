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
  html { font-size:16px; }
  html, body { background:var(--page); }
  body {
    margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
    color:var(--text); padding:24px;
    font-family:'Manrope',system-ui,-apple-system,'Segoe UI',sans-serif; -webkit-font-smoothing:antialiased;
  }
  .card {
    width:100%; max-width:380px; background:var(--surface); border:1px solid var(--border);
    border-radius:14px; padding:36px 36px 40px; box-shadow:0 6px 30px rgba(17,23,34,.08);
  }
  .brand { display:flex; align-items:center; gap:9px; font-weight:800; font-size:1.05rem; letter-spacing:.03em; }
  .brand .dot { width:11px; height:11px; border-radius:50%; background:var(--accent); flex-shrink:0; }
  h1 { font-size:1.55rem; line-height:1.15; margin:16px 0 6px; }
  .sub { color:var(--text-3); font-size:0.9rem; margin-bottom:20px; }
  label { display:block; font-size:0.8rem; font-weight:600; color:var(--text-2); margin:14px 0 6px; }
  input {
    width:100%; background:var(--field); border:1px solid var(--field-border); border-radius:9px;
    padding:11px 13px; font-size:0.95rem; color:var(--text); font-family:inherit;
  }
  input:focus-visible { outline:2px solid var(--accent); outline-offset:1px; background:#fff; }
  button {
    width:100%; margin-top:22px; background:var(--accent); color:#fff; border:none; border-radius:9px;
    padding:12px; font-size:1rem; font-weight:700; cursor:pointer; font-family:inherit;
  }
  button:hover { background:var(--accent-dark); }
  .msg { border-radius:8px; padding:10px 13px; font-size:0.85rem; font-weight:600; margin-bottom:2px; }
  .msg.err { background:var(--critical-soft); color:var(--critical); }
  .msg.info { background:var(--good-soft); color:var(--good); }
  .switch { margin-top:18px; text-align:center; font-size:0.9rem; color:var(--text-3); }
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
