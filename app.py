    mid = (request.form.get('media_id') or request.form.get('selected_media') or '').strip()
    link = request.form.get('reel_url', '').strip()
    if not link and not mid.isdigit():
        flash('Reels bağlantısı yapıştırın veya listeden video seçin.')
        return redirect(url_for('index'))
    plans = []
    mode = request.form.get('message_mode', 'single')
    if mode == 'plans':
        for i in range(6):
            trigger = request.form.get(f'plan_trigger_{i}', '').strip()
            dm = request.form.get(f'plan_dm_{i}', '').strip()
            reply = request.form.get(f'plan_reply_{i}', '').strip()
            if not any((trigger, dm, reply)):
                continue
            if not all((trigger, dm, reply)) or len(trigger)>100 or len(dm)>1000 or len(reply)>1000:
                flash('Her plan için yorum, DM ve yorum yanıtı dolu olmalı. Mesajlar en fazla 1000 karakter.')
                return redirect(url_for('index', edit=mid))
            plans.append({'trigger': trigger, 'dm_variants': [x.strip() for x in dm.split('---') if x.strip()], 'reply_variants': [x.strip() for x in reply.split('---') if x.strip()]})
        if not plans or any(not p['dm_variants'] or not p['reply_variants'] for p in plans) or len({normalized(p['trigger']) for p in plans}) != len(plans):
            flash('En az bir dolu plan ekleyin; yorum tetikleyicileri farklı olmalı.')
            return redirect(url_for('index', edit=mid))
    dms = [request.form.get(f'dm_{i}', '').strip() for i in range(4)]
    replies = list(dict.fromkeys(x.strip() for x in request.form.get('replies', '').splitlines() if x.strip()))
    keywords = list(dict.fromkeys(x.strip() for x in request.form.get('keywords', '').splitlines() if x.strip()))
    if mode != 'plans' and (any(not x or len(x) > 1000 for x in dms) or len(set(dms)) != 4 or len(replies) < 2 or any(len(x) > 1000 for x in replies)):
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
        old_plans = {normalized(p['trigger']):p for p in old.get('plans', [])}
        for plan in plans:
            plan['created_at'] = old_plans.get(normalized(plan['trigger']), {}).get('created_at', time.time())
        item = {'media_id': mid, 'title': media.get('caption', '')[:100],
              'keywords': keywords if not plans else [], 'dm_variants': dms if not plans else [], 'reply_variants': replies if not plans else [], 'plans': plans,
              'permalink': media.get('permalink', ''),
              'follower_only': request.form.get('audience', 'followers') != 'everyone',
              'created_at': old.get('created_at', time.time()), 'enabled': old.get('enabled', True)}
        cloud('PUT', 'automation_v2/rules/' + mid, item)
        update_cached_rule(mid, item)
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
            item['enabled'] = not item.get('enabled', False)
            cloud('PATCH', path, {'enabled': item['enabled']})
            update_cached_rule(mid, item)
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
        update_cached_rule(mid, None)
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
