"""Print (and show) the link for reaching the dashboard from a phone.

    ./.venv/bin/python connect.py

The LAN address changes whenever the router hands out a new lease, so this
recomputes it rather than trusting anything written down earlier.
"""

import socket
import subprocess
import sys
from pathlib import Path

from config import config


def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))       # nothing is sent; this just picks the route
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return ""


def main() -> int:
    token_path = Path(config.dashboard_token_file)
    if not token_path.exists():
        print("  אין עדיין טוקן — הפעל את server.py פעם אחת והוא ייווצר.")
        return 1

    token = token_path.read_text().strip()
    ip = lan_ip()
    if not ip:
        print("  לא הצלחתי לזהות כתובת ברשת המקומית — ודא שאתה מחובר ל-Wi-Fi.")
        return 1

    port = config.dashboard_port
    url = f"http://{ip}:{port}/?token={token}"

    print()
    print("  סרוק את ה-QR שנפתח, או פתח בטלפון את הקישור:")
    print(f"  {url}")
    print()
    print(f"  אחרי הכניסה הראשונה מספיק:  http://{ip}:{port}")
    print("  (הטלפון חייב להיות על אותה רשת Wi-Fi)")
    print()

    try:
        import segno
    except ImportError:
        print("  להצגת QR:  ./.venv/bin/pip install segno")
        return 0

    out = Path("data/connect-qr.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    segno.make(url, error="m").save(out, scale=9, border=3)
    if sys.platform == "darwin":
        subprocess.run(["open", str(out)], check=False)
    print(f"  QR נשמר ונפתח: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
