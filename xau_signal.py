#!/usr/bin/env python3
"""Adaptive market watcher for GitHub Actions: XAUUSD when fresh, BTCUSDT otherwise.

Scans CLOSED M5/M15 bars, sends an entry/SL/TP alert to Telegram when a setup
appears with RR >= 1.3 (user's hard minimum). Empty stdout = silence.

Sources: gold = Yahoo GC=F (may go stale on holidays/weekends),
         btc  = Binance public mirror data-api.binance.vision.
State/dedup: XAU_STATE (default ~/.local/share/xau-signal/state.json).
Credentials: TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID from env or
~/.config/xau-signal.env (KEY=VALUE lines).
Flags: --test (labelled delivery test), --debug (diagnostics, never sends),
       --dry-run (print alert instead of sending).
"""
import json
import os
import sys
import time
import urllib.request

RR = 1.3                 # пользовательский минимум: 1.2 и ниже не используем
COOLDOWN_S = 3600        # не чаще одного алерта в час
SAME_SETUP_S = 3600      # тот же сетап рядом с прежним входом — пропуск
STATE = os.environ.get('XAU_STATE', os.path.expanduser('~/.local/share/xau-signal/state.json'))
TZ_OFFSET_H = 4.5        # локальное время пользователя UTC+04:30

ASSETS = {
    'gold': dict(label='XAUUSD (золото)', point=0.1, min_move=10.0, src='Yahoo GC=F'),
    'btc':  dict(label='BTCUSDT (биткоин)', point=1.0, min_move=None, src='Binance'),
}
TV_SRC = {'gold': ('cfd', 'TVC:GOLD'), 'btc': ('crypto', 'BINANCE:BTCUSDT')}


class Stale(Exception):
    pass


def load_env():
    p = os.path.expanduser('~/.config/xau-signal.env')
    if os.path.exists(p):
        for line in open(p):
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())


def send_telegram(text):
    tok = os.environ.get('TELEGRAM_BOT_TOKEN')
    chat = os.environ.get('TELEGRAM_CHAT_ID')
    if not tok or not chat:
        print('ERROR: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set', file=sys.stderr)
        return False
    data = json.dumps({'chat_id': chat, 'text': text, 'parse_mode': 'HTML',
                       'disable_web_page_preview': True}).encode()
    req = urllib.request.Request(f'https://api.telegram.org/bot{tok}/sendMessage',
                                 data=data, headers={'Content-Type': 'application/json'})
    try:
        r = json.load(urllib.request.urlopen(req, timeout=30))
    except Exception as e:
        print(f'telegram transport error: {e}', file=sys.stderr)
        return False
    if not r.get('ok'):
        print(f'telegram error: {r}', file=sys.stderr)
    return bool(r.get('ok'))


def fetch_gold(interval, rng, tries=3):
    url = (f'https://query1.finance.yahoo.com/v8/finance/chart/GC=F'
           f'?interval={interval}&range={rng}')
    for a in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            d = json.load(urllib.request.urlopen(req, timeout=20))['chart']['result'][0]
            ts = d.get('timestamp') or []
            q = d['indicators']['quote'][0]
            bars = [(ts[i], q['open'][i], q['high'][i], q['low'][i], q['close'][i])
                    for i in range(len(ts))
                    if None not in (q['open'][i], q['high'][i], q['low'][i], q['close'][i])]
            return bars, d['meta'].get('regularMarketPrice')
        except Exception:
            if a == tries - 1:
                raise
            time.sleep(3)


def fetch_btc(interval, limit=300):
    url = (f'https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT'
           f'&interval={interval}&limit={limit}')
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    d = json.load(urllib.request.urlopen(req, timeout=20))
    return [(int(r[0]) // 1000, float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in d]


def tv_verdict(asset):
    """Independent TradingView confirmation (1H/4H/1D); failure never blocks an alert."""
    ep, tk = TV_SRC[asset]
    payload = {"symbols": {"tickers": [tk]},
               "columns": ["Recommend.All|60", "Recommend.All|240", "Recommend.All"]}
    req = urllib.request.Request(f'https://scanner.tradingview.com/{ep}/scan',
                                 data=json.dumps(payload).encode(),
                                 headers={'User-Agent': 'Mozilla/5.0',
                                          'Content-Type': 'application/json'})
    return json.load(urllib.request.urlopen(req, timeout=15))['data'][0]['d']


def ema(v, n):
    k = 2 / (n + 1)
    e = v[0]
    out = [e]
    for x in v[1:]:
        e = x * k + e * (1 - k)
        out.append(e)
    return out


def rsi_series(cl, n=14):
    if len(cl) < n + 1:
        return [None] * len(cl)
    out = [None] * n
    g = l = 0.0
    for i in range(1, n + 1):
        d = cl[i] - cl[i - 1]
        g += max(d, 0); l += max(-d, 0)
    ag, al = g / n, l / n
    out.append(100.0 if al == 0 else 100 - 100 / (1 + ag / al))
    for i in range(n + 1, len(cl)):
        d = cl[i] - cl[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
        out.append(100.0 if al == 0 else 100 - 100 / (1 + ag / al))
    return out


def macd_hist(cl):
    e12, e26 = ema(cl, 12), ema(cl, 26)
    line = [a - b for a, b in zip(e12, e26)]
    sig = ema(line, 9)
    return [a - b for a, b in zip(line, sig)]


def atr(bars, n=14):
    trs = [max(bars[i][2] - bars[i][3], abs(bars[i][2] - bars[i - 1][4]),
               abs(bars[i][3] - bars[i - 1][4])) for i in range(1, len(bars))]
    a = sum(trs[:n]) / n
    for t in trs[n:]:
        a = (a * (n - 1) + t) / n
    return a


def closed(bars, sec):
    now = time.time()
    return bars[:-1] if bars and bars[-1][0] + sec > now else bars


def analyze(bars):
    cl = [b[4] for b in bars]
    return {
        'close': cl[-1],
        'ema20': ema(cl, 20)[-1], 'ema50': ema(cl, 50)[-1],
        'ema200': ema(cl, 200)[-1] if len(cl) >= 200 else None,
        'rsi': rsi_series(cl)[-1], 'rsi5ago': rsi_series(cl)[-6],
        'hist': macd_hist(cl), 'atr': atr(bars),
        'hi10': max(b[2] for b in bars[-10:]), 'lo10': min(b[3] for b in bars[-10:]),
        'hi20': max(b[2] for b in bars[-21:-1]), 'lo20': min(b[3] for b in bars[-21:-1]),
        'close5ago': cl[-6],
    }


def cross_down(h):
    return h[-1] < 0 <= h[-2]


def cross_up(h):
    return h[-1] > 0 >= h[-2]


def detect(a5, a15, price, min_move):
    """Return (dir, entry, sl, tp, reason) or None. Closed bars only."""
    atr5 = a5['atr']
    h5 = a5['hist']
    up15 = a15['ema20'] > a15['ema50'] and (a15['ema200'] is None or a15['close'] > a15['ema200'])
    dn15 = a15['ema20'] < a15['ema50'] and (a15['ema200'] is None or a15['close'] < a15['ema200'])

    def mk(dirn, sl, reason):
        risk = (sl - price) if dirn == 'SHORT' else (price - sl)
        if risk <= 0 or risk > 3 * atr5:
            return None
        move = max(min_move, RR * risk)
        tp = price - move if dirn == 'SHORT' else price + move
        return dirn, price, sl, tp, reason

    if dn15 and abs(a5['close'] - a5['ema50']) < 0.6 * atr5 and 50 <= a5['rsi'] <= 72 and cross_down(h5):
        r = mk('SHORT', a5['hi10'] + 0.3 * atr5, 'M15 тренд вниз, откат M5 к EMA50, MACD M5 вниз')
        if r: return r
    if up15 and abs(a5['close'] - a5['ema50']) < 0.6 * atr5 and 28 <= a5['rsi'] <= 50 and cross_up(h5):
        r = mk('LONG', a5['lo10'] - 0.3 * atr5, 'M15 тренд вверх, откат M5 к EMA50, MACD M5 вверх')
        if r: return r
    if a5['close'] < a5['lo20'] and h5[-1] < h5[-2] < 0 and a5['rsi'] < 45 and not up15:
        r = mk('SHORT', a5['lo20'] + 0.5 * atr5, 'Пробой минимума 20 баров M5, импульс вниз')
        if r: return r
    if a5['close'] > a5['hi20'] and h5[-1] > h5[-2] > 0 and a5['rsi'] > 55 and not dn15:
        r = mk('LONG', a5['hi20'] - 0.5 * atr5, 'Пробой максимума 20 баров M5, импульс вверх')
        if r: return r
    if a15['rsi'] < 25 and a5['close'] < a5['close5ago'] and a5['rsi'] > a5['rsi5ago'] and cross_up(h5):
        r = mk('LONG', a5['lo10'] - 0.5 * atr5, 'Перепроданность M15 + бычья дивергенция M5, MACD вверх')
        if r: return r
    if a15['rsi'] > 75 and a5['close'] > a5['close5ago'] and a5['rsi'] < a5['rsi5ago'] and cross_down(h5):
        r = mk('SHORT', a5['hi10'] + 0.5 * atr5, 'Перекупленность M15 + медвежья дивергенция M5, MACD вниз')
        if r: return r
    return None


def fmt(asset, sig, price, test=False):
    cfg = ASSETS[asset]
    dirn, entry, sl, tp, reason = sig
    risk = abs(entry - sl)
    move = abs(tp - entry)
    pts = int(round(move / cfg['point']))
    head = '🧪 <b>ТЕСТ сигнала (проверка доставки)</b>\n' if test else ''
    emoji = '🔻' if dirn == 'SHORT' else '🔺'
    tv = ''
    try:
        r = tv_verdict(asset)
        if r and len(r) >= 3:
            tv = f"📡 TV: 1H {r[0]:+.2f} · 4H {r[1]:+.2f} · 1D {r[2]:+.2f}\n"
    except Exception:
        pass
    return (f"{head}{emoji} <b>{cfg['label']} — сетап {dirn}</b> (M5/M15)\n"
            f"📍 Вход: <b>{entry:.2f}</b>\n"
            f"🛑 Стоп-лосс: <b>{sl:.2f}</b> (риск ${risk:.1f})\n"
            f"🎯 Тейк-профит: <b>{tp:.2f}</b> (${move:.1f} = {pts} пунктов)\n"
            f"⚖️ R:R 1:{RR}\n"
            f"📊 {reason}\n"
            f"🕐 {time.strftime('%H:%M %d.%m', time.gmtime(time.time() + TZ_OFFSET_H * 3600))} "
            f"| live {price:.2f} | {cfg['src']}\n"
            f"{tv}"
            f"⚠️ Технический сетап, не гарантия. Стоп обязателен.")


def load_state():
    try:
        if os.path.exists(STATE):
            return json.load(open(STATE))
    except Exception:
        pass
    return {}


def save_state(st):
    os.makedirs(os.path.dirname(STATE) or '.', exist_ok=True)
    json.dump(st, open(STATE, 'w'))


def main():
    load_env()
    test = '--test' in sys.argv
    debug = '--debug' in sys.argv
    dry = '--dry-run' in sys.argv
    if test:
        msg = fmt('btc', ('LONG', 60286.0, 59900.0, 60640.0, 'пример: отскок от перепроданности'), 60286.0, test=True)
        if dry:
            print(msg)
        else:
            send_telegram(msg)
        return 0

    now = time.time()
    try:
        b5, price = fetch_gold('5m', '5d')
        if not b5 or now - b5[-1][0] > 1800:
            raise Stale('gold data stale')
        b15, _ = fetch_gold('15m', '5d')
        asset = 'gold'
        min_move = ASSETS['gold']['min_move']
    except Exception:
        try:
            b5 = fetch_btc('5m')
            b15 = fetch_btc('15m')
            price = b5[-1][4]
            asset = 'btc'
            min_move = max(1.0, price * 0.003)
        except Exception as e:
            print(f'fetch failed: {e}', file=sys.stderr)
            return 0  # never alert on error state

    b5, b15 = closed(b5, 300), closed(b15, 900)
    if len(b5) < 210 or len(b15) < 210:
        return 0
    if now - b5[-1][0] > 1800:
        if debug: print(f'{asset}: stale — silent')
        return 0
    a5, a15 = analyze(b5), analyze(b15)
    if debug:
        print(f"asset={asset} price={price:.2f} min_move={min_move:.1f} RR={RR}")
        print(f"  M5 RSI={a5['rsi']:.1f} EMA20/50={a5['ema20']:.2f}/{a5['ema50']:.2f} "
              f"hist={a5['hist'][-1]:.2f} hi20/lo20={a5['hi20']:.2f}/{a5['lo20']:.2f}")
        print(f"  M15 RSI={a15['rsi']:.1f} EMA20/50/200={a15['ema20']:.2f}/{a15['ema50']:.2f}/"
              f"{a15['ema200'] and round(a15['ema200'], 2)}")
    sig = detect(a5, a15, price, min_move)
    if not sig:
        if debug: print('no setup')
        return 0

    st = load_state()
    dirn, entry = sig[0], sig[1]
    if now - st.get('ts', 0) < COOLDOWN_S:
        if debug: print('cooldown — skip')
        return 0
    same_limit = 3.0 if asset == 'gold' else 0.001 * price
    if asset == st.get('asset') and dirn == st.get('dir') and abs(entry - st.get('entry', 0)) < same_limit \
            and now - st.get('ts', 0) < SAME_SETUP_S:
        if debug: print('same setup — skip')
        return 0

    msg = fmt(asset, sig, price)
    if dry:
        print(msg)
        return 0
    if not send_telegram(msg):
        return 0  # delivery failed: don't burn cooldown, retry next run
    save_state({'ts': now, 'asset': asset, 'dir': dirn,
                'entry': entry, 'sl': sig[2], 'tp': sig[3]})
    return 0


if __name__ == '__main__':
    sys.exit(main())
