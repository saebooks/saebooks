"""Cold-path verification for the one-click server. Stdlib only.

Usage: python verify_oneclick.py <web_base> <api_base> [options]

Options (each adds v0.4 checks on top of the original seven):
  --log <file>        scan the server's console log: no bearer-token value
                      may appear, and the startup banner must be present
  --bind-check        every server port must be bound to 127.0.0.1 only
                      (parses `ss -tlnp`; Linux in-VM check)
  --pdf               request a report PDF; 200+%PDF passes, a clean fast
                      502 "LaTeX service" answer reports KNOWNGAP (one-click
                      bundles no latex-api), anything else fails

Exit 0 = all checks pass (KNOWNGAP does not fail). Prints one line per check.
"""
import json
import re
import sys
import time
import urllib.error
import urllib.request

WEB, API = sys.argv[1], sys.argv[2]
OPTS = sys.argv[3:]
LOGFILE = OPTS[OPTS.index("--log") + 1] if "--log" in OPTS else None
EMAIL, PASSWORD = "you@example.com", "change-me-now"


def get(url, token=None, method="GET", body=None, headers=None):
    req = urllib.request.Request(url, method=method)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, data, timeout=10) as r:
        return r.status, r.read()


def check(name, ok, detail=""):
    print(("PASS" if ok else "FAIL"), name, detail)
    if not ok:
        sys.exit(1)


for _ in range(120):
    try:
        s1 = get(API + "/api/v1/healthz")[0]
        s2 = get(WEB + "/healthz")[0]
        if s1 == 200 and s2 == 200:
            break
    except Exception:
        pass
    time.sleep(1)
else:
    check("healthz", False, "not healthy within 120s")
check("healthz", True)

st, body = get(WEB + "/static/tailwind.css")
check("tailwind.css", st == 200 and len(body) > 10000, f"{st} {len(body)}B")

st, body = get(API + "/api/v1/auth/login", method="POST",
               body={"email": EMAIL, "password": PASSWORD})
tok = json.loads(body)["access_token"]
check("login", st == 200)

st, body = get(API + "/api/v1/accounts?limit=300", tok)
items = json.loads(body)["items"]
bank = next(a["id"] for a in items if a.get("code") == "1-1110")
exp = next(a["id"] for a in items if a.get("code") == "6-1000")
check("accounts", len(items) > 100, f"{len(items)} accounts")

st, body = get(API + "/api/v1/journal_entries", tok, "POST", {
    "entry_date": "2026-07-21",
    "memo": "one-click cold-path verification",
    "lines": [
        {"account_id": exp, "debit": "42.00", "credit": "0"},
        {"account_id": bank, "debit": "0", "credit": "42.00"},
    ],
})
je = json.loads(body)
check("journal create", st in (200, 201), je.get("status", ""))

st, body = get(API + "/api/v1/journal_entries/" + je["id"], tok)
ver = str(json.loads(body)["version"])
st, body = get(API + "/api/v1/journal_entries/" + je["id"] + "/post", tok,
               "POST", headers={"If-Match": ver})
check("journal post", json.loads(body).get("status") == "POSTED")

st, body = get(API + "/api/v1/reports/trial_balance?as_of=2026-07-21", tok)
tb = json.loads(body)
check("trial balance", tb["balanced"] and tb["total_debits"] == tb["total_credits"],
      f"debits={tb['total_debits']} credits={tb['total_credits']}")

# ---- v0.4 additions ---------------------------------------------------------

if LOGFILE:
    text = open(LOGFILE, encoding="utf-8", errors="replace").read()
    leaked = re.findall(r"Bearer\s+[A-Za-z0-9_\-]{20,}", text)
    check("no token in console", not leaked,
          f"{len(leaked)} bearer value(s) leaked" if leaked else "log clean")
    check("startup banner", "Starting SAE Books" in text,
          "first-run progress line present" if "Starting SAE Books" in text
          else "banner missing from log")

if "--bind-check" in OPTS:
    import subprocess

    out = subprocess.run(["ss", "-tlnp"], capture_output=True, text=True).stdout
    ports = {WEB.rsplit(":", 1)[1], API.rsplit(":", 1)[1], "18962"}
    loopback = ("127.0.0.1:", "[::ffff:127.0.0.1]:", "[::1]:")
    bad = [ln for ln in out.splitlines()
           if any(":" + p + " " in ln + " " for p in ports)
           and not any(lb in ln for lb in loopback)]
    check("loopback-only bind", not bad, "; ".join(bad) or "all on 127.0.0.1")

if "--pdf" in OPTS:
    t0 = time.monotonic()
    try:
        st, body = get(API + "/api/v1/reports/trial_balance.pdf?as_of_date=2026-07-21", tok)
    except urllib.error.HTTPError as e:
        st, body = e.code, e.read()
    took = time.monotonic() - t0
    if st == 200 and body[:5] == b"%PDF-" and len(body) > 5000:
        check("pdf render", True, f"{len(body)}B PDF")
    elif st == 502 and b"LaTeX service" in body and took < 15:
        print("KNOWNGAP pdf render — clean 502 in "
              f"{took:.1f}s (one-click bundles no latex-api)")
    else:
        check("pdf render", False, f"status={st} took={took:.1f}s body={body[:120]!r}")

print("ALL CHECKS PASSED")
