"""Reels yorum otomasyonu. Kurulum için KURULUM.md dosyasını okuyun."""
import argparse
import hashlib
import hmac
import logging
import os
import random
import re
import secrets
import threading
import time
from collections import deque
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from flask import Flask, abort, flash, redirect, render_template_string, request, session, url_for

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY') or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                  SESSION_COOKIE_SECURE=os.getenv('COOKIE_SECURE') == '1', MAX_CONTENT_LENGTH=65536)
ACCESS_TOKEN = os.getenv('ACCESS_TOKEN', '')
IG_USER_ID = os.getenv('IG_USER_ID', '')
FIREBASE_URL = os.getenv('FIREBASE_URL', '').rstrip('/')
FIREBASE_AUTH = os.getenv('FIREBASE_AUTH', '')
API_VERSION = os.getenv('META_API_VERSION', '')
LOGIN_TYPE = os.getenv('META_LOGIN_TYPE', 'facebook')
GRAPH_HOST = 'https://graph.instagram.com' if LOGIN_TYPE == 'instagram' else 'https://graph.facebook.com'
PANEL_USER = os.getenv('PANEL_USER', 'admin')
PANEL_PASSWORD = os.getenv('PANEL_PASSWORD', '')
POLL_SECONDS = max(15, int(os.getenv('POLL_SECONDS', '30')))
SEND_DELAY = max(0, int(os.getenv('SEND_DELAY_SECONDS', '15')))
STOP = threading.Event()
EVENTS = deque(maxlen=50)
EVENT_LOCK = threading.Lock()
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')


class ServiceError(Exception):
    pass


def event(message):
    logging.info(message)
    with EVENT_LOCK:
        EVENTS.appendleft(time.strftime('%H:%M:%S') + ' — ' + message)


def validate_config():
    missing = [name for name, value in [('ACCESS_TOKEN', ACCESS_TOKEN), ('IG_USER_ID', IG_USER_ID),
               ('FIREBASE_URL', FIREBASE_URL), ('META_API_VERSION', API_VERSION),
               ('PANEL_PASSWORD', PANEL_PASSWORD), ('SECRET_KEY', os.getenv('SECRET_KEY'))] if not value]
    if missing:
        raise SystemExit('Eksik ortam değişkenleri: ' + ', '.join(missing))
    if LOGIN_TYPE not in ('facebook', 'instagram') or not re.fullmatch(r'v\d+\.\d+', API_VERSION):
        raise SystemExit('META_LOGIN_TYPE veya META_API_VERSION geçersiz.')
    if urlparse(FIREBASE_URL).scheme != 'https':
        raise SystemExit('FIREBASE_URL HTTPS olmalı.')


def graph(method, path, *, params=None, payload=None):
    """Gönderimlerde otomatik HTTP tekrarı yok: timeout sonrası sonuç belirsizdir."""
    try:
        res = requests.request(method, f'{GRAPH_HOST}/{API_VERSION}/{path}',
            headers={'Authorization': 'Bearer ' + ACCESS_TOKEN}, params=params,
            json=payload, timeout=(5, 25))
        data = res.json()
    except (requests.RequestException, ValueError):
        raise ServiceError('Meta bağlantı hatası; gönderim yapıldıysa sonuç belirsiz olabilir.') from None
    if not res.ok or not isinstance(data, dict) or 'error' in data:
        error = data.get('error', {}) if isinstance(data, dict) else {}
        raise ServiceError(f'Meta HTTP {res.status_code}, kod {error.get("code", "?")}, alt kod {error.get("error_subcode", "?")}')
    return data


def pages(path, fields):
    cursor = None
    seen = set()
    while True:
        params = {'fields': fields, 'limit': 100}
        if cursor:
            params['after'] = cursor
        data = graph('GET', path, params=params)
        yield from data.get('data', [])
        paging = data.get('paging', {})
        cursor = paging.get('cursors', {}).get('after')
        if not paging.get('next') or not cursor or cursor in seen:
            break
        seen.add(cursor)


def cloud(method, path, value=None, headers=None):
    try:
        res = requests.request(method, f'{FIREBASE_URL}/{path}.json',
            params={'auth': FIREBASE_AUTH} if FIREBASE_AUTH else None,
            json=value, headers=headers, timeout=(5, 20))
        if res.status_code == 412:
            return None, None, False
        if not res.ok:
            raise ServiceError(f'Firebase HTTP {res.status_code}; okuma/yazma başarısız.')
        return res.json(), res.headers.get('ETag'), True
    except (requests.RequestException, ValueError):
        raise ServiceError('Firebase bağlantı hatası.') from None


def rules():
    data = cloud('GET', 'automation_v2/rules')[0] or {}
    if not isinstance(data, dict):
        raise ServiceError('Firebase kural biçimi geçersiz.')
    return data


def normalized(text):
    return text.replace('İ', 'i').replace('I', 'ı').casefold()


def matches(text, keywords):
    return not keywords or any(normalized(k) in normalized(text) for k in keywords)


def follower_state(user_id):
    if not user_id:
        return None
    try:
        value = graph('GET', str(user_id), params={'fields': 'is_user_follow_business'}).get('is_user_follow_business')
        return value if isinstance(value, bool) else None
    except ServiceError:
        return None


def comment_time(comment):
    try:
        return datetime.fromisoformat(comment['timestamp'].replace('Z', '+00:00')).timestamp()
    except (KeyError, ValueError, TypeError):
        return None


def history_path(comment_id):
    return 'automation_v2/history/' + hashlib.sha256(str(comment_id).encode()).hexdigest()


def process_comment(media_id, comment, rule):
    cid = str(comment.get('id', ''))
    author = str((comment.get('from') or {}).get('id', ''))
    created = comment_time(comment)
    now = time.time()
    if not cid or author == IG_USER_ID or created is None:
        return
    if created < rule['created_at'] or now - created > 7 * 86400:
        return
    if not matches(comment.get('text', ''), rule.get('keywords', [])):
        return
    # Eski uygulamanın işlediği yorumlar tekrar gönderilmez.
    if cloud('GET', 'history/' + cid)[0]:
        return
    path = history_path(cid)
    previous, etag, _ = cloud('GET', path, headers={'X-Firebase-ETag': 'true'})
    if previous and (previous.get('status') != 'blocked' or previous.get('retry_at', 0) > now):
        return
    state = {'comment_id': cid, 'media_id': media_id, 'status': 'checking',
             'updated_at': now, 'dm_status': 'pending', 'reply_status': 'pending'}
    # Firebase ETag rezervasyonu: bir yorumu yalnızca bir çalışan alabilir.
    if not cloud('PUT', path, state, {'if-match': etag})[2]:
        return
    if STOP.wait(SEND_DELAY):
        state.update(status='blocked', retry_at=0)
        cloud('PUT', path, state)
        return
    # Kural kaldırılmışsa/durdurulmuşsa gecikmeden sonra gönderme.
    current = cloud('GET', 'automation_v2/rules/' + media_id)[0]
    if not current or not current.get('enabled'):
        state.update(status='cancelled')
        cloud('PUT', path, state)
        return
    rule = current
    if created < rule['created_at'] or not matches(comment.get('text', ''), rule.get('keywords', [])):
        state.update(status='cancelled')
        cloud('PUT', path, state)
        return
    follows = follower_state(author)
    if follows is not True:
        state.update(status='blocked', retry_at=time.time() + 300,
                     reason='Takip etmiyor' if follows is False else 'Takip durumu doğrulanamadı')
        cloud('PUT', path, state)
        event(f'Yorum {cid}: {state["reason"]}; DM ve yanıt gönderilmedi.')
        return
    state.update(dm_text=random.choice(rule['dm_variants']),
                 reply_text=random.choice(rule['reply_variants']))
    # Yan etkiden önce niyet kaydedilir. Belirsiz sonuç otomatik tekrarlanmaz.
    state.update(status='sending', dm_status='sending')
    cloud('PUT', path, state)
    try:
        result = graph('POST', IG_USER_ID + '/messages', payload={
            'recipient': {'comment_id': cid}, 'message': {'text': state['dm_text']}})
        if not result.get('message_id'):
            raise ServiceError('Meta mesaj kimliği dönmedi; sonuç belirsiz.')
        state.update(dm_status='sent', message_id=result['message_id'])
        cloud('PUT', path, state)
        state.update(reply_status='sending')
        cloud('PUT', path, state)
        result = graph('POST', cid + '/replies', payload={'message': state['reply_text']})
        if not result.get('id'):
            raise ServiceError('Meta yanıt kimliği dönmedi; sonuç belirsiz.')
        state.update(status='done', reply_status='sent', reply_id=result['id'])
        cloud('PUT', path, state)
        event(f'Yorum {cid}: DM ve yorum yanıtı gönderildi.')
    except ServiceError as exc:
        state.update(status='review_required', reason=str(exc))
        # Başarısızlık veya belirsizlik başarı olarak kaydedilmez.
        cloud('PUT', path, state)
        event(f'Yorum {cid}: kontrol gerekli — {exc}')


def background_bot_loop():
    event('Bot başladı. Yalnızca seçilen videolar ve doğrulanmış takipçiler işlenecek.')
    while not STOP.is_set():
        try:
            for mid, rule in rules().items():
                if not rule.get('enabled'):
                    continue
                try:
                    for comment in pages(mid + '/comments', 'id,text,timestamp,from'):
                        if STOP.is_set():
                            return
                        process_comment(mid, comment, rule)
                except ServiceError as exc:
                    event(f'Video {mid}: {exc}')
        except ServiceError as exc:
            event(str(exc))
        except Exception as exc:
            # Exception metni URL/token içerebilir; yalnızca türü yazılır.
            event('Bot hatası: ' + type(exc).__name__)
        STOP.wait(POLL_SECONDS)


@app.before_request
def protect_panel():
    auth = request.authorization
    if not PANEL_PASSWORD or not auth or not (
        hmac.compare_digest((auth.username or '').encode(), PANEL_USER.encode()) and
        hmac.compare_digest((auth.password or '').encode(), PANEL_PASSWORD.encode())):
        return 'Panel için giriş gerekli.', 401, {'WWW-Authenticate': 'Basic realm="Reels Panel"'}
    session.setdefault('csrf', secrets.token_urlsafe(32))
    if request.method == 'POST' and not hmac.compare_digest(request.form.get('csrf', ''), session['csrf']):
        abort(403)


HTML = '''<!doctype html><html lang="tr"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Reels Otomasyonu</title>
<style>body{font-family:system-ui;background:#f3f5fa;color:#202b42;margin:0;padding:24px}main{max-width:900px;margin:auto}section{background:white;padding:24px;border-radius:14px;margin:20px 0}input,textarea,select{box-sizing:border-box;width:100%;padding:10px;margin:8px 0 16px;border:1px solid #bdc6d6;border-radius:6px}button{background:#315ae8;color:white;padding:10px 18px;border:0;border-radius:6px;cursor:pointer}pre{white-space:pre-wrap;overflow-wrap:anywhere}a{color:#315ae8}.note{background:#fff1cc;padding:14px}.inline{display:inline-block;margin:6px}label{display:block}small{display:block;color:#536077} .flash{padding:12px;background:#e5ecff}</style>
<main><h1>Reels yorum → DM paneli</h1>
<p class="note">Yalnızca takip ettiği API ile doğrulanan kişilere gönderilir. Takip durumu okunamayan takipçiler de atlanır. Her yorum için dört DM seçeneğinden biri ve bir yorum yanıtı seçilir.</p>
{% for msg in get_flashed_messages() %}<p class="flash">{{ msg }}</p>{% endfor %}
<section><h2>{{ 'Kuralı düzenle' if edit else 'Video ekle' }}</h2>
{% if media_error %}<p>{{ media_error }} Video ID’sini elle girebilirsiniz.</p>{% endif %}
<form action="{{ url_for('save_rule') }}" method="post"><input type="hidden" name="csrf" value="{{ session.csrf }}">
{% if edit %}<input type="hidden" name="media_id" value="{{ edit.media_id }}"><p>Video ID: {{ edit.media_id }}</p>
{% else %}<label>Hesabınızdaki Reels</label><select name="selected_media"><option value="">Bir video seçin veya ID girin</option>{% for m in media %}<option value="{{ m.id }}">{{ (m.caption or 'Başlıksız')[:90] }} — {{ m.id }}</option>{% endfor %}</select>
<label>Veya Instagram medya ID’si (Reels bağlantısındaki kısa kod değildir)</label><input name="media_id" pattern="[0-9]+" placeholder="Örnek: 18012345678901234">{% endif %}
<label>Anahtar kelimeler (her satıra bir tane; boş bırakırsanız bütün yeni yorumlar)</label><textarea name="keywords" rows="3">{{ edit.keywords|join('\n') if edit else '' }}</textarea>
{% for i in range(4) %}<label>DM seçeneği {{ i+1 }}</label><textarea name="dm_{{ i }}" required maxlength="1000" rows="2">{{ edit.dm_variants[i] if edit else '' }}</textarea>{% endfor %}
<label>Yorum yanıtları (her satıra bir alternatif; en az iki farklı yanıt)</label><textarea name="replies" required rows="4">{{ edit.reply_variants|join('\n') if edit else '' }}</textarea>
<small>Örnek: “Bilgileri DM’den ilettim.” ve “Mesaj kutunu kontrol edebilirsin.” Yanıt yalnızca DM başarıyla gönderildikten sonra yazılır.</small><p>Takipçi filtresi her zaman açık. Yeni kural, kaydedildiği andan sonraki yorumlarda çalışır.</p><button>Kaydet</button></form></section>
<section><h2>Eklenen videolar</h2>{% for mid,r in rules.items() %}<article><h3>{{ r.title or mid }}</h3><p>Video ID: {{ mid }} · {{ 'Aktif' if r.enabled else 'Durduruldu' }}</p><p>Anahtar kelimeler: {{ r.keywords|join(', ') or 'Bütün yeni yorumlar' }}</p>
<a href="{{ url_for('index', edit=mid) }}">Düzenle</a>
<form class="inline" method="post" action="{{ url_for('toggle_rule') }}"><input type="hidden" name="csrf" value="{{ session.csrf }}"><input type="hidden" name="media_id" value="{{ mid }}"><button>{{ 'Durdur' if r.enabled else 'Başlat' }}</button></form>
<form class="inline" method="post" action="{{ url_for('delete_rule') }}"><input type="hidden" name="csrf" value="{{ session.csrf }}"><input type="hidden" name="media_id" value="{{ mid }}"><button>Sil</button></form></article>{% else %}<p>Henüz video eklenmedi.</p>{% endfor %}</section>
<section><h2>Son işlemler</h2><p>Firebase: automation_v2/history altında kalıcı gönderim durumları tutulur. Aşağıdaki liste bu sürecin son kayıtlarıdır; sayfayı yenileyin.</p>{% for e in events %}<pre>{{ e }}</pre>{% else %}<p>Bu süreçte kayıt yok. Ayrı bot çalışanının kayıtları terminalinde görünür.</p>{% endfor %}</section></main></html>'''


@app.get('/')
def index():
    try:
        saved = rules()
    except ServiceError as exc:
        return str(exc), 503
    media, error = [], ''
    try:
        media = [m for m in pages(IG_USER_ID + '/media', 'id,caption,media_product_type,permalink')
                 if m.get('media_product_type') == 'REELS']
    except ServiceError as exc:
        error = str(exc)
    edit = saved.get(request.args.get('edit'))
    with EVENT_LOCK:
        entries = list(EVENTS)
    return render_template_string(HTML, rules=saved, media=media, edit=edit,
                                  media_error=error, events=entries)


@app.post('/save')
def save_rule():
    mid = (request.form.get('media_id') or request.form.get('selected_media') or '').strip()
    if not mid.isdigit():
        flash('Geçerli bir medya ID’si girin veya video seçin.')
        return redirect(url_for('index'))
    dms = [request.form.get(f'dm_{i}', '').strip() for i in range(4)]
    replies = list(dict.fromkeys(x.strip() for x in request.form.get('replies', '').splitlines() if x.strip()))
    keywords = list(dict.fromkeys(x.strip() for x in request.form.get('keywords', '').splitlines() if x.strip()))
    if any(not x or len(x) > 1000 for x in dms) or len(set(dms)) != 4 or len(replies) < 2 or any(len(x) > 1000 for x in replies):
        flash('Dört farklı ve dolu DM seçeneği, en az iki farklı yorum yanıtı gerekli. Her metin en fazla 1000 karakter.')
        return redirect(url_for('index', edit=mid))
    try:
        # Yalnızca hesaba ait Reels seçilmesine izin verilir.
        media = next((m for m in pages(IG_USER_ID + '/media', 'id,caption,media_product_type') if str(m['id']) == mid), None)
        if not media or media.get('media_product_type') != 'REELS':
            flash('Bu ID hesabınızda erişilebilen bir Reels videosuna ait değil.')
            return redirect(url_for('index'))
        old = cloud('GET', 'automation_v2/rules/' + mid)[0] or {}
        cloud('PUT', 'automation_v2/rules/' + mid, {'media_id': mid, 'title': media.get('caption', '')[:100],
              'keywords': keywords, 'dm_variants': dms, 'reply_variants': replies,
              'created_at': old.get('created_at', time.time()), 'enabled': old.get('enabled', True)})
        flash('Video kuralı kaydedildi.')
    except ServiceError as exc:
        flash(str(exc))
    return redirect(url_for('index'))


@app.post('/toggle')
def toggle_rule():
    mid = request.form.get('media_id', '')
    if not mid.isdigit():
        abort(400)
    try:
        path = 'automation_v2/rules/' + mid
        item = cloud('GET', path)[0]
        if item:
            cloud('PATCH', path, {'enabled': not item.get('enabled', False)})
    except ServiceError as exc:
        flash(str(exc))
    return redirect(url_for('index'))


@app.post('/delete')
def delete_rule():
    mid = request.form.get('media_id', '')
    if not mid.isdigit():
        abort(400)
    try:
        cloud('DELETE', 'automation_v2/rules/' + mid)
        flash('Video kuralı silindi; gönderim geçmişi korundu.')
    except ServiceError as exc:
        flash(str(exc))
    return redirect(url_for('index'))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker', action='store_true', help='Yalnızca bot çalışanını başlatır')
    args = parser.parse_args()
    validate_config()
    if args.worker:
        try:
            background_bot_loop()
        except KeyboardInterrupt:
            STOP.set()
    else:
        threading.Thread(target=background_bot_loop, daemon=True).start()
        # Reloader kapalı: aynı bot iki kez başlatılmaz.
        from waitress import serve
        try:
            serve(app, host='0.0.0.0', port=int(os.getenv('PORT', '5000')), threads=4)
        finally:
            STOP.set()
