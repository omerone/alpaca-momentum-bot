# Momentum Trading Bot

בוט מסחר אלגוריתמי המבוסס על **מומנטום** עם **trailing stop loss**, מחובר ל-**Alpaca Paper Trading** (חשבון דמו חינמי).

## איך זה עובד

1. **סריקת מומנטום** — הבוט סורק רשימת מניות ומחפש מניות שעולות (+% מינימלי) עם נפח מסחר גבוה מהממוצע
2. **כניסה** — קונה מניות עם המומנטום החזק ביותר
3. **Stop Loss ראשוני** — מגדיר stop loss % מתחת למחיר הכניסה
4. **Trailing Stop** — כשהמניה עולה, ה-stop loss עולה איתה (נשאר X% מתחת לשיא)
5. **יציאה** — כשהמחיר יורד ל-stop loss, מוכר אוטומטית

## התקנה

### 1. יצירת חשבון Alpaca Paper Trading (חינמי)

1. הירשם ב-[Alpaca Markets](https://app.alpaca.markets/signup)
2. עבור ל-[Paper Trading Dashboard](https://app.alpaca.markets/paper/dashboard/overview)
3. צור API Keys (API Key + Secret Key)

### 2. התקנת הבוט

```bash
cd "trading bot"
python -m venv venv
venv\Scripts\activate        # Windows
pip install -r requirements.txt
```

### 3. הגדרת API Keys

```bash
copy .env.example .env
```

ערוך את `.env` והוסף את המפתחות שלך:

```
ALPACA_API_KEY=PKxxxxxxxx
ALPACA_SECRET_KEY=xxxxxxxx
ALPACA_PAPER=true
```

### 4. הרצה

```bash
python main.py
```

## הגדרות (config.py)

| פרמטר | ברירת מחדל | תיאור |
|--------|------------|--------|
| `min_price_change_pct` | 2.0% | עלייה מינימלית לכניסה |
| `min_volume_ratio` | 1.5x | נפח מסחר vs ממוצע 20 יום |
| `max_positions` | 5 | מקסימום פוזיציות פתוחות |
| `position_size_usd` | $1,000 | גודל כל עסקה |
| `initial_stop_loss_pct` | 3.0% | stop loss ראשוני |
| `trailing_stop_pct` | 2.0% | trailing stop מתחת לשיא |
| `min_profit_to_trail_pct` | 1.0% | רווח מינימלי לפני trailing |
| `scan_interval_seconds` | 60 | תדירות סריקה |
| `monitor_interval_seconds` | 15 | תדירות עדכון stops |

## מבנה הפרויקט

```
trading bot/
├── main.py          # לולאה ראשית
├── config.py        # הגדרות
├── scanner.py       # סורק מומנטום
├── strategy.py      # trailing stop loss
├── broker.py        # חיבור Alpaca
├── requirements.txt
├── .env.example
└── README.md
```

## אזהרה

זהו בוט לימודי/דמו בלבד. מסחר אמיתי כרוך בסיכון. בדוק תמיד ב-paper trading לפני שימוש בכסף אמיתי.
