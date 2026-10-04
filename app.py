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
    """Gönderimlerde otomatik tekrar yok; hata çıktısında token/yanıt gövdesi yok."""
    uncertain = ' Gönderim sonucu belirsiz olabilir; otomatik tekrar yapılmaz.' if method != 'GET' else ''
    try:
        res = requests.request(method, f'{GRAPH_HOST}/{API_VERSION}/{path}',
            headers={'Authorization': 'Bearer ' + ACCESS_TOKEN}, params=params,
            json=payload, timeout=(5, 25))
            headers={'Authorization': 'Bearer ' + ACCESS_TOKEN.strip()}, params=params,
            json=payload, timeout=(10, 45))
    except requests.RequestException as exc:
        kind = type(exc).__name__
        hints = {'ConnectTimeout': 'Meta sunucusuna bağlantı zaman aşımı.',
                 'ReadTimeout': 'Meta yanıtı beklenirken zaman aşımı.',
                 'SSLError': 'Meta bağlantısında TLS sertifika hatası.',
                 'ConnectionError': 'Meta sunucusuna ağ/DNS bağlantısı kurulamadı.'}
        raise ServiceError(hints.get(kind, 'Meta ağ isteği başarısız.') + ' Hata türü: ' + kind + '.' + uncertain) from None
    try:
        data = res.json()
    except (requests.RequestException, ValueError):
        raise ServiceError('Meta bağlantı hatası; gönderim yapıldıysa sonuç belirsiz olabilir.') from None
    except ValueError:
        raise ServiceError(f'Meta HTTP {res.status_code}: JSON olmayan yanıt döndü. ' +
                           ('İstek sınırı uygulanmış olabilir.' if res.status_code == 429 else 'Meta geçici hata veya erişim engeli döndürmüş olabilir.') + uncertain) from None
    if not res.ok or not isinstance(data, dict) or 'error' in data:
        error = data.get('error', {}) if isinstance(data, dict) else {}
        raise ServiceError(f'Meta HTTP {res.status_code}, kod {error.get("code", "?")}, alt kod {error.get("error_subcode", "?")}')
        if not isinstance(error, dict):
            error = {}
        code = error.get('code', '?')
        hints = {190: 'Token geçersiz veya süresi dolmuş olabilir.',
                 100: 'Hesap ID’si, API alanı veya istek parametresi kabul edilmedi.',
                 10: 'Bu işlem için izin/erişim koşulları sağlanmıyor.',
                 200: 'Bu işlem için izin/erişim koşulları sağlanmıyor.',
                 4: 'API istek sınırına ulaşıldı.', 17: 'API istek sınırına ulaşıldı.'}
        raise ServiceError(f'Meta HTTP {res.status_code}, kod {code}, alt kod {error.get("error_subcode", "?")}. ' + hints.get(code, '') + uncertain)
    return data


def reel_code(value):
    """Paylaşım linkini güvenli biçimde normalize eder; uzak URL'yi ziyaret etmez."""
    value = (value or '').strip()
    if value and '://' not in value:
        value = 'https://' + value
    parsed = urlparse(value)
    if parsed.scheme != 'https' or parsed.hostname not in ('instagram.com', 'www.instagram.com', 'm.instagram.com'):
        raise ServiceError('Instagram Reels bağlantısı girin: https://www.instagram.com/reel/…/')
    match = re.fullmatch(r'/(?:reel|reels|p)/([A-Za-z0-9_-]+)/?', parsed.path)
    if not match:
        raise ServiceError('Videonun bağlantısını kullanın; profil veya kısa paylaşım yönlendirmesi uygun değil.')
    return match.group(1)


def account_reels():
    # Ayrı sorgu: bazı hesaplarda ürün türü alanı bulunmasa da video kaybolmaz.
    for item in pages(IG_USER_ID + '/media', 'id,caption,media_type,permalink'):
        link = item.get('permalink', '')
        if '/reel/' in link or '/reels/' in link:
            yield item
        elif item.get('media_type') == 'VIDEO':
            try:
                detail = graph('GET', str(item['id']), params={'fields': 'media_product_type'})
            except ServiceError:
                continue
            if detail.get('media_product_type') == 'REELS':
                yield item


def media_diagnostic(exc=None):
    message = str(exc) if exc else 'Meta hesabınız için erişilebilir Reels döndürmedi.'
    return message + ' Render’da META_LOGIN_TYPE=instagram ve IG_USER_ID değerini, tokenın bağlı hesabını ve instagram_business_basic iznini kontrol edin. Bağlantı eklemek eksik API iznini gidermez.'


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
    follows = follower_state(author) if rule.get('follower_only', True) else True
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
    event('Bot başladı. Seçilen videolar, her kuralın hedef kitle ayarına göre işlenecek.')
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


HTML = '''<!doctype html><html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>LGSHocam · Reels Stüdyo</title>
<style>
:root{--ink:#18243d;--muted:#6d7890;--line:#e6eaf3;--accent:#6559ed}*{box-sizing:border-box}body{margin:0;background:#f5f6fc;color:var(--ink);font:15px/1.6 system-ui,-apple-system,sans-serif}header{background:#fff;border-bottom:1px solid var(--line)}nav{max-width:1180px;margin:auto;padding:20px 28px;display:flex;justify-content:space-between;align-items:center}.brand{font-weight:800;font-size:20px;letter-spacing:-.5px}.logo{display:inline-grid;place-items:center;background:var(--accent);color:#fff;border-radius:12px;width:40px;height:40px;margin-right:10px}.tag,.pill{display:inline-block;border-radius:30px;padding:5px 12px;background:#eeebff;color:#5e50be;font-size:12px;font-weight:700}.pill.green{background:#e8f8ef;color:#28784f}.pill.grey{background:#edf0f5;color:#6a758c}main{max-width:1180px;margin:auto;padding:32px 28px}.hero{padding:30px 34px;background:linear-gradient(110deg,#292247,#5145a2);border-radius:22px;color:#fff;position:relative;overflow:hidden}.hero h1{font-size:30px;margin:4px 0 10px;letter-spacing:-1px}.hero p{color:#d6d1ef;max-width:670px;margin:0}.eyebrow{font-size:11px;letter-spacing:2px;font-weight:800;color:#c4bbff}.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin:22px 0}.stat{background:#fff;border:1px solid var(--line);border-radius:14px;padding:18px 22px}.stat strong{display:block;font-size:25px}.stat small{color:var(--muted)}.layout{display:grid;grid-template-columns:minmax(0,1.4fr) minmax(0,1fr);gap:24px;align-items:start}.card{background:#fff;border:1px solid var(--line);border-radius:18px;padding:26px;margin-bottom:22px;box-shadow:0 4px 20px #24254a04}h2{font-size:20px;margin:0 0 6px;letter-spacing:-.4px}h3{font-size:16px;margin:0 0 6px}.sub{color:var(--muted);font-size:13px;margin:0 0 22px}.step{color:var(--accent);font-size:11px;font-weight:800;letter-spacing:1px;margin:25px 0 8px}.step:first-of-type{margin-top:0}label{display:block;font-weight:650;font-size:13px;margin-bottom:7px}input,textarea,select{width:100%;font:inherit;font-size:14px;border:1px solid #dce1ee;background:#fafbfe;border-radius:10px;padding:12px;outline:none;margin-bottom:14px;color:var(--ink)}input:focus,textarea:focus,select:focus{border-color:var(--accent);box-shadow:0 0 0 3px #6559ed16}textarea{resize:vertical}small.help{display:block;color:var(--muted);font-size:12px;margin:-4px 0 16px}.divider{border:0;border-top:1px solid var(--line);margin:22px 0}.dmgrid{display:grid;grid-template-columns:1fr 1fr;gap:0 14px}.audiences{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}.audiences label{cursor:pointer;border:1px solid var(--line);border-radius:12px;padding:14px;background:#fafbfe;font-weight:500;margin:0}.audiences label:has(input:checked){border-color:var(--accent);background:#f1efff}.audiences input{width:auto;margin:0 6px 0 0;accent-color:var(--accent)}.audiences small{display:block;color:var(--muted);font-size:11px;margin-top:5px}button,.button{display:inline-block;font:inherit;font-size:13px;font-weight:650;border:0;border-radius:9px;padding:10px 16px;background:var(--accent);color:white;text-decoration:none;cursor:pointer}button:hover,.button:hover{filter:brightness(.95)}.save{width:100%;padding:14px;font-size:15px}.ghost{background:#f0eefc;color:#6254c4}.danger{background:#fff0f0;color:#b45151}.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}.actions form{margin:0}.video{padding:18px 0;border-bottom:1px solid var(--line)}.video:last-child{border:0}.videohead{display:flex;justify-content:space-between;gap:12px;align-items:start}.video h3{overflow-wrap:anywhere}.video p{font-size:12px;color:var(--muted);margin:7px 0}.empty{text-align:center;padding:30px 12px;background:#fafbfe;border:1px dashed #dce1ee;border-radius:12px;color:var(--muted)}.empty b{display:block;color:var(--ink);margin-bottom:6px}.alert{padding:14px 16px;border-radius:12px;background:#fff5df;color:#7d601b;font-size:13px;margin-bottom:18px;overflow-wrap:anywhere}.flash{background:#edeaff;color:#5749af}.logs{max-height:320px;overflow:auto}.log{font-size:12px;padding:11px 0;border-bottom:1px solid var(--line);overflow-wrap:anywhere}.tiny{color:var(--muted);font-size:11px}a{color:var(--accent)}footer{text-align:center;color:var(--muted);font-size:11px;margin-top:24px}@media(max-width:850px){.layout{grid-template-columns:1fr}main{padding:20px 16px}.hero{padding:24px}.hero h1{font-size:25px}nav{padding:16px}.stats{gap:8px}.stat{padding:14px}.stat strong{font-size:21px}.card{padding:20px}}@media(max-width:450px){.dmgrid,.audiences{grid-template-columns:1fr}.tag{display:none}}
</style></head><body><header><nav><div class="brand"><span class="logo">L</span>LGSHocam <span style="font-weight:400;color:#8992a6">/ Stüdyo</span></div><span class="tag">Reels otomasyonu</span></nav></header><main>
<div class="hero"><span class="eyebrow">YORUMLARDAN SOHBETLERE</span><h1>Bir yorumla iletişim başlasın.</h1><p>Reels bağlantını ekle, mesajlarını hazırla ve kime ulaşacağını seç. Videolarının otomasyonunu tek yerden yönet.</p></div>
<div class="stats"><div class="stat"><strong>{{ rules|length }}</strong><small>Eklenen video</small></div><div class="stat"><strong>{{ rules.values()|selectattr('enabled')|list|length }}</strong><small>Aktif kural</small></div><div class="stat"><strong>{{ media|length }}</strong><small>Hesaptan bulunan Reels</small></div></div>
{% for msg in get_flashed_messages() %}<div class="alert flash" role="status">{{ msg }}</div>{% endfor %}
<div class="layout"><section class="card"><h2>{{ 'Kuralını düzenle' if edit else 'Yeni otomasyon oluştur' }}</h2><p class="sub">Her videoya kendi mesajlarını ve hedef kitlesini tanımla.</p>
{% if media_error %}<div class="alert" role="alert"><b>Videolar listelenemedi</b><br>{{ media_error }}</div>{% endif %}
<form action="{{ url_for('save_rule') }}" method="post"><input type="hidden" name="csrf" value="{{ session.csrf }}">
<div class="step">01 · VİDEONU SEÇ</div>
{% if edit %}<input type="hidden" name="media_id" value="{{ edit.media_id }}"><p>{{ edit.title or 'Reels videosu' }}</p><small class="help">Video ID: {{ edit.media_id }}</small>
{% else %}<label for="reel_url">Reels bağlantısı</label><input id="reel_url" name="reel_url" type="url" placeholder="https://www.instagram.com/reel/…/" autocomplete="off"><small class="help">Instagram’da videoyu aç → Paylaş → Bağlantıyı kopyala. ID bulmana gerek yok.</small>
{% if media %}<label for="selected_media">Veya hesabındaki videolardan seç</label><select id="selected_media" name="selected_media"><option value="">Bir Reels seç</option>{% for m in media %}<option value="{{ m.id }}">{{ (m.caption or 'Başlıksız Reels')[:85] }}</option>{% endfor %}</select>{% endif %}
<details><summary class="tiny">Medya ID’si ile ekle</summary><input name="media_id" inputmode="numeric" pattern="[0-9]+" placeholder="Instagram medya ID’si"></details>{% endif %}
<div class="step">02 · HEDEF KİTLE</div><div class="audiences"><label><input type="radio" name="audience" value="followers" {% if not edit or edit.get('follower_only', True) %}checked{% endif %}>Yalnızca takipçiler<small>Takip ettiği doğrulanan kişiler</small></label><label><input type="radio" name="audience" value="everyone" {% if edit and not edit.get('follower_only', True) %}checked{% endif %}>Herkes<small>Takip şartı olmadan uygun yorumlar</small></label></div>
<small class="help">Takipçi modunda API takip durumunu okuyamazsa gönderim yapılmaz. Herkes modunda takip kontrolü atlanır.</small>
<label for="keywords">Hangi yorumlar tetiklesin?</label><textarea id="keywords" name="keywords" rows="2" placeholder="MATEMATİK&#10;NOTLAR">{{ edit.keywords|join('\n') if edit else '' }}</textarea><small class="help">Her satıra bir anahtar kelime. Boşsa bütün yeni yorumlar.</small>
<div class="step">03 · DM MESAJLARI</div><p class="sub">Dört farklı metin hazırla. Her yorumda bunlardan biri rastgele seçilir.</p><div class="dmgrid">{% for i in range(4) %}<div><label for="dm_{{ i }}">Mesaj {{ i+1 }}</label><textarea id="dm_{{ i }}" name="dm_{{ i }}" required maxlength="1000" rows="4" placeholder="Göndermek istediğin mesajı ve linkini yaz…">{{ edit.dm_variants[i] if edit else '' }}</textarea></div>{% endfor %}</div>
<div class="step">04 · YORUM YANITLARI</div><label for="replies">Alternatif cevapların</label><textarea id="replies" name="replies" required rows="4" placeholder="Bilgileri DM’den ilettim, kontrol edebilirsin.&#10;Mesaj kutuna bir göz at!">{{ edit.reply_variants|join('\n') if edit else '' }}</textarea><small class="help">Her satıra bir yanıt, en az iki farklı seçenek. Yanıt yalnızca DM başarıyla gönderilirse yazılır.</small><hr class="divider"><button class="save">{{ 'Değişiklikleri kaydet' if edit else 'Otomasyonu kaydet' }}</button><p class="tiny">Yeni kurallar kaydedildiği andan sonraki yorumlarda çalışır.</p></form></section>
<aside><section class="card"><h2>Video koleksiyonun</h2><p class="sub">Kurallarını düzenle, durdur veya yeniden başlat.</p>{% for mid,r in rules.items() %}<article class="video"><div class="videohead"><h3>{{ r.title or 'Reels videosu' }}</h3><span class="pill {{ 'green' if r.enabled else 'grey' }}">{{ 'Aktif' if r.enabled else 'Duraklatıldı' }}</span></div><p>{{ 'Yalnızca takipçiler' if r.get('follower_only', True) else 'Herkes' }} · {{ r.keywords|join(', ') or 'Bütün yeni yorumlar' }}</p>{% if r.permalink %}<a href="{{ r.permalink }}" target="_blank" rel="noopener noreferrer" class="tiny">Videoyu Instagram’da aç ↗</a>{% endif %}<div class="actions"><a class="button ghost" href="{{ url_for('index', edit=mid) }}">Düzenle</a><form method="post" action="{{ url_for('toggle_rule') }}"><input type="hidden" name="csrf" value="{{ session.csrf }}"><input type="hidden" name="media_id" value="{{ mid }}"><button class="ghost">{{ 'Durdur' if r.enabled else 'Başlat' }}</button></form><form method="post" action="{{ url_for('delete_rule') }}"><input type="hidden" name="csrf" value="{{ session.csrf }}"><input type="hidden" name="media_id" value="{{ mid }}"><button class="danger">Sil</button></form></div></article>{% else %}<div class="empty"><b>İlk videonla başla</b>Reels linkini soldaki forma yapıştır.<br>Kaydettiğin videolar burada görünecek.</div>{% endfor %}</section>
<section class="card"><div class="videohead"><h2>Son işlemler</h2><a class="tiny" href="{{ url_for('index') }}">Yenile ↻</a></div><p class="sub">Gönderimleri ve takip kontrollerini buradan izle.</p><div class="logs">{% for e in events %}<div class="log">{{ e }}</div>{% else %}<div class="empty">Henüz bir işlem kaydı yok.</div>{% endfor %}</div><p class="tiny">Bu süreçteki son kayıtlar gösterilir. Kalıcı gönderim geçmişi Firebase’de tutulur.</p></section></aside></div><footer>LGSHocam Stüdyo · Her uygun yorum için bir DM ve bir yorum yanıtı</footer></main></body></html>
'''


@app.get('/')
def index():
    try:
        saved = rules()
    except ServiceError as exc:
        return str(exc), 503
    media, error = [], ''
    try:
        media = list(account_reels())
        if not media:
            error = media_diagnostic()
    except ServiceError as exc:
        error = media_diagnostic(exc)
    edit = saved.get(request.args.get('edit'))
    with EVENT_LOCK:
        entries = list(EVENTS)
    return render_template_string(HTML, rules=saved, media=media, edit=edit,
                                  media_error=error, events=entries)


@app.post('/save')
def save_rule():
    mid = (request.form.get('media_id') or request.form.get('selected_media') or '').strip()
    link = request.form.get('reel_url', '').strip()
    if not link and not mid.isdigit():
        flash('Reels bağlantısı yapıştırın veya listeden video seçin.')
        return redirect(url_for('index'))
    dms = [request.form.get(f'dm_{i}', '').strip() for i in range(4)]
    replies = list(dict.fromkeys(x.strip() for x in request.form.get('replies', '').splitlines() if x.strip()))
    keywords = list(dict.fromkeys(x.strip() for x in request.form.get('keywords', '').splitlines() if x.strip()))
    if any(not x or len(x) > 1000 for x in dms) or len(set(dms)) != 4 or len(replies) < 2 or any(len(x) > 1000 for x in replies):
        flash('Dört farklı ve dolu DM seçeneği, en az iki farklı yorum yanıtı gerekli. Her metin en fazla 1000 karakter.')
        return redirect(url_for('index', edit=mid))
    try:
        # Yalnızca hesaba ait Reels seçilmesine izin verilir.
        code = reel_code(link) if link else None
        media = None
        for candidate in account_reels():
            if code:
                try:
                    found = reel_code(candidate.get('permalink', '')) == code
                except ServiceError:
                    found = False
            else:
                found = str(candidate['id']) == mid
            if found:
                media = candidate
                break
        if not media:
            flash('Bu video bağlı hesabın erişilebilir Reels listesinde bulunamadı. Hesap ID’si, token ve izinleri kontrol edin.')
            return redirect(url_for('index'))
        mid = str(media['id'])
        old = cloud('GET', 'automation_v2/rules/' + mid)[0] or {}
        cloud('PUT', 'automation_v2/rules/' + mid, {'media_id': mid, 'title': media.get('caption', '')[:100],
              'keywords': keywords, 'dm_variants': dms, 'reply_variants': replies,
              'permalink': media.get('permalink', ''),
              'follower_only': request.form.get('audience', 'followers') != 'everyone',
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
