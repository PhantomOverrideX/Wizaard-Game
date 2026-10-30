#!/usr/bin/env python3
"""Emberwild Realms launcher + co-op relay server (Python 3 only, no packages).

    python play.py                    play alone on this computer
    python play.py --lan              host co-op: friends on your network can join
    python play.py --lan --password x protect the room with a password
    python play.py --port 9000        use another port
    python play.py --no-browser       don't open a browser tab

How co-op works (and why it is light on slow computers):
  * This server only RELAYS small messages between players. It runs no game logic.
  * Every enemy is simulated by the player who spawned it; everyone else shows a cheap copy.
  * Camps, bosses and dungeons are 'claimed' here so only one player's computer runs each one; when that
    player leaves, the server picks who takes over. It also sends each player only the enemies near them,
    keeps the day/night clock, weather and which camps/bosses are cleared, and saves them (world_state.json).
Press Ctrl+C to stop.
"""
import argparse, asyncio, base64, hashlib, json, mimetypes, os, random, socket, struct, subprocess, sys, threading, time, urllib.error, urllib.parse, urllib.request, webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
DAY_LENGTH = 420.0
mimetypes.add_type('application/javascript', '.js')
mimetypes.add_type('application/octet-stream', '.hdr')
mimetypes.add_type('application/json', '.json')
MAX_FRAME = 1 << 17
NEAR = 260.0
MOB_NEAR = 285.0  # events (hits, casts, projectiles...) are only sent to players this close
SPATIAL = {'ah', 'cast', 'shot', 'tele', 'toast'}
RELAY = {'p', 'look', 'chat', 'mb', 'rm', 'mdie', 'ah', 'tele', 'shot', 'toast', 'cast', 'flag'}
DIRECT = {'mh', 'hurt'}
WEATHER = [('clear', 5), ('cloudy', 3), ('rain', 3), ('storm', 1), ('snow', 1), ('blizzard', 1), ('sandstorm', 1)]


# ---------------------------------------------------------------------------------------------
# AUTO-UPDATER: keeps this folder in step with the game's GitHub repository.
# On every start it asks GitHub for the newest commit; if it differs from what is installed it
# downloads only the files that changed, checks each one against GitHub's own hash, then swaps
# them in. If anything goes wrong (offline, rate limit, bad download) it just plays what you have.
# ---------------------------------------------------------------------------------------------
REPO = 'PhantomOverrideX/Wizaard-Game'
API_BASE = os.environ.get('EW_API_BASE', 'https://api.github.com')
RAW_BASE = os.environ.get('EW_RAW_BASE', 'https://raw.githubusercontent.com')
STATE_FILE = os.path.join(ROOT, '.update_state.json')
VERSION_FILE = os.path.join(ROOT, 'version.json')
KEEP_LOCAL = {'world_state.json', '.update_state.json', 'version.json'}  # never overwritten or deleted
UA = {'User-Agent': 'EmberwildRealms-launcher'}


def _get(url, token=None, timeout=25, accept=None):
    h = dict(UA)
    if token: h['Authorization'] = 'Bearer ' + token
    if accept: h['Accept'] = accept
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
        return r.read()


def _blob_sha(data):
    return hashlib.sha1(b'blob %d\0' % len(data) + data).hexdigest()


def _safe_rel(rel):
    """Only plain relative paths that stay inside this folder are ever written."""
    if not rel or rel.startswith('/') or '\\' in rel or ':' in rel: return False
    parts = rel.split('/')
    return all(p not in ('', '.', '..') and not p.startswith('.') for p in parts)


def _load_state():
    try:
        with open(STATE_FILE) as f: return json.load(f)
    except Exception:
        return {}


def _save_state(st):
    try:
        with open(STATE_FILE, 'w') as f: json.dump(st, f)
    except OSError:
        pass


def installed_version():
    try:
        with open(VERSION_FILE) as f: return json.load(f)
    except Exception:
        return {}


def check_for_update(args):
    """Returns 'updated-self' (launcher replaced: caller must restart), 'updated', 'current' or 'skipped'."""
    if args.no_update or os.environ.get('EW_UPDATED') == '1': return 'skipped'
    if os.path.isdir(os.path.join(ROOT, '.git')):
        print('Update: this folder is a git checkout - skipping auto-update (use git pull).'); return 'skipped'
    repo = args.repo or REPO; token = args.token or os.environ.get('GITHUB_TOKEN') or None; ref = args.branch or 'HEAD'
    print('Checking for game updates (%s)...' % repo, flush=True)
    try:
        commit = json.loads(_get('%s/repos/%s/commits/%s' % (API_BASE, repo, urllib.parse.quote(ref)), token, accept='application/vnd.github+json'))
        sha = commit['sha']; tree_sha = commit['commit']['tree']['sha']
        msg = (commit['commit'].get('message') or '').split('\n')[0][:80]; date = (commit['commit'].get('committer') or {}).get('date', '')[:10]
    except urllib.error.HTTPError as ex:
        why = 'GitHub rate limit reached - try again in a while' if ex.code in (403, 429) else ('repository not found (is it private? set GITHUB_TOKEN)' if ex.code == 404 else 'HTTP %d' % ex.code)
        print('Update check failed: %s. Starting the version you have.' % why); return 'skipped'
    except Exception as ex:
        print('Update check failed (%s). Starting the version you have.' % (getattr(ex, 'reason', None) or ex)); return 'skipped'
    st = _load_state()
    if st.get('sha') == sha and not args.force_update:
        print('Game is up to date (%s).' % sha[:7]); return 'current'
    try:
        tree = json.loads(_get('%s/repos/%s/git/trees/%s?recursive=1' % (API_BASE, repo, tree_sha), token, timeout=40, accept='application/vnd.github+json'))
        if tree.get('truncated'): raise RuntimeError('repository too large to list')
        blobs = [t for t in tree['tree'] if t.get('type') == 'blob']
    except Exception as ex:
        print('Could not read the file list (%s). Starting the version you have.' % ex); return 'skipped'
    idx = sorted((t['path'] for t in blobs if t['path'].split('/')[-1] == 'index.html'), key=lambda p: p.count('/'))
    if not idx:
        print('The repository has no index.html yet, so there is no game to download. Starting the version you have.'); return 'skipped'
    prefix = idx[0][:-len('index.html')]  # the game may live in a sub-folder of the repo
    want = {}
    for t in blobs:
        p = t['path']
        if not p.startswith(prefix): continue
        rel = p[len(prefix):]
        if rel in KEEP_LOCAL or rel.endswith('.bak') or not _safe_rel(rel): continue
        want[rel] = (t['sha'], p)
    todo = []
    for rel, (bsha, p) in sorted(want.items()):
        full = os.path.join(ROOT, *rel.split('/'))
        try:
            with open(full, 'rb') as f: same = _blob_sha(f.read()) == bsha
        except OSError:
            same = False
        if not same: todo.append(rel)
    print('New version %s available (%s%s) - downloading %d file(s)...' % (sha[:7], date + ': ' if date else '', msg, len(todo)), flush=True)
    stage = os.path.join(ROOT, '.update_tmp'); import shutil
    shutil.rmtree(stage, ignore_errors=True)
    try:
        for i, rel in enumerate(todo, 1):
            bsha, p = want[rel]; url = '%s/%s/%s/%s' % (RAW_BASE, repo, sha, urllib.parse.quote(p))
            data = None
            for attempt in range(3):
                try:
                    data = _get(url, token, timeout=60)
                    if _blob_sha(data) == bsha: break
                    data = None
                except Exception:
                    data = None
                time.sleep(1 + attempt)
            if data is None: raise RuntimeError('could not download %s' % rel)
            if rel == 'play.py':
                import ast; ast.parse(data.decode('utf-8'))  # never install a launcher that does not even parse
            dest = os.path.join(stage, *rel.split('/')); os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, 'wb') as f: f.write(data)
            if i % 10 == 0 or i == len(todo): print('  %d/%d' % (i, len(todo)), flush=True)
    except Exception as ex:
        shutil.rmtree(stage, ignore_errors=True); print('Update failed (%s). Nothing was changed - starting the version you have.' % ex); return 'skipped'
    # everything downloaded and verified: swap it in (launcher last)
    new_self = 'play.py' in todo
    for rel in [r for r in todo if r != 'play.py']:
        dest = os.path.join(ROOT, *rel.split('/')); os.makedirs(os.path.dirname(dest), exist_ok=True); os.replace(os.path.join(stage, *rel.split('/')), dest)
    old = st.get('files') or []
    for rel in old:  # remove files the repo has since deleted (only ones this updater installed)
        if rel not in want and rel not in KEEP_LOCAL and _safe_rel(rel):
            try: os.remove(os.path.join(ROOT, *rel.split('/')))
            except OSError: pass
    if new_self:
        try: shutil.copy2(os.path.join(ROOT, 'play.py'), os.path.join(ROOT, 'play.py.bak'))
        except OSError: pass
        os.replace(os.path.join(stage, 'play.py'), os.path.join(ROOT, 'play.py'))
    shutil.rmtree(stage, ignore_errors=True)
    _save_state({'sha': sha, 'files': sorted(want), 'repo': repo})
    try:
        with open(VERSION_FILE, 'w') as f: json.dump({'sha': sha, 'short': sha[:7], 'date': date, 'message': msg}, f)
    except OSError:
        pass
    print('Updated to %s.' % sha[:7], flush=True)
    return 'updated-self' if new_self else 'updated'


class Player:
    def __init__(self, pid, writer):
        self.id = pid; self.w = writer; self.name = 'Wizard'; self.look = {}; self.x = 0.0; self.z = 0.0; self.inst = ''; self.ok = False


class World:
    def __init__(self, args):
        self.args = args; self.players = {}; self.claims = {}; self.flags = {}; self.next_id = 1
        self.t0 = time.time(); self.weather = 'clear'
        self.path = None if args.fresh else os.path.join(ROOT, args.save)
        if self.path and os.path.isfile(self.path):  # the server keeps the shared world between runs
            try:
                with open(self.path) as f: st = json.load(f)
                self.t0 = time.time() - float(st.get('T', 0)); self.flags = dict(st.get('flags', {})); self.weather = st.get('weather', 'clear')
                print('Loaded the saved world (day %d, %d camp/boss records).' % (1 + int(0.3 + (time.time() - self.t0) / DAY_LENGTH), len(self.flags)))
            except Exception as ex:
                print('Could not read %s (%s) - starting a fresh world.' % (args.save, ex))

    def save(self):
        if not self.path: return
        try:
            tmp = self.path + '.tmp'
            with open(tmp, 'w') as f: json.dump({'T': self.clock(), 'flags': self.flags, 'weather': self.weather}, f)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def clock(self):
        return time.time() - self.t0


W = None


def ws_frame(payload: bytes, op=0x1):
    n = len(payload)
    if n < 126: head = struct.pack('!BB', 0x80 | op, n)
    elif n < 65536: head = struct.pack('!BBH', 0x80 | op, 126, n)
    else: head = struct.pack('!BBQ', 0x80 | op, 127, n)
    return head + payload


async def ws_read(reader):
    """Return (opcode, payload) for one complete message (handles fragmentation)."""
    msg = b''; first = None
    while True:
        h = await reader.readexactly(2)
        fin = h[0] & 0x80; op = h[0] & 0x0F; ln = h[1] & 0x7F; masked = h[1] & 0x80
        if ln == 126: ln = struct.unpack('!H', await reader.readexactly(2))[0]
        elif ln == 127: ln = struct.unpack('!Q', await reader.readexactly(8))[0]
        if ln > MAX_FRAME: raise ConnectionError('frame too large')
        mask = await reader.readexactly(4) if masked else b''
        data = await reader.readexactly(ln) if ln else b''
        if masked: data = bytes(b ^ mask[i & 3] for i, b in enumerate(data)) if ln < 512 else _unmask(data, mask)
        if op in (0x8, 0x9, 0xA): return op, data
        if first is None: first = op
        msg += data
        if len(msg) > MAX_FRAME: raise ConnectionError('message too large')
        if fin: return first, msg


def _unmask(data, mask):
    n = len(data); m = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(data, 'big') ^ int.from_bytes(m, 'big')).to_bytes(n, 'big')


def send(p, obj, droppable=False):
    try:
        if droppable and p.w.transport.get_write_buffer_size() > (1 << 19): return  # slow client: skip non-critical updates
        p.w.write(ws_frame(json.dumps(obj, separators=(',', ':')).encode()))
    except Exception:
        pass


def broadcast(obj, skip=None, near=None, droppable=False):
    for p in list(W.players.values()):
        if not p.ok or p is skip: continue
        if near is not None and (abs(p.x - near[0]) > NEAR or abs(p.z - near[1]) > NEAR): continue
        send(p, obj, droppable)


def inst_left(who, inst_id):
    """Someone left a dungeon/cave (or disconnected). If others are still inside, name an heir who takes over
    the enemies; otherwise free the dungeon so the next visitor gets a fresh one."""
    if not inst_id: return
    key = 'inst:' + inst_id
    inside = sorted(p.id for p in W.players.values() if p.ok and p is not who and p.inst == inst_id)
    if inside:
        heir = inside[0]
        if W.claims.get(key) in (None, who.id) or W.claims.get(key) not in W.players:
            W.claims[key] = heir
            broadcast({'t': 'adopt', 'inst': inst_id, 'heir': heir, 'old': who.id})
            for p in W.players.values():
                if p.ok: send(p, {'t': 'claimed', 'k': key, 'ok': p.id == heir, 'o': heir})
    elif W.claims.get(key) in (None, who.id):
        W.claims.pop(key, None); broadcast({'t': 'rel', 'k': key})


def filter_mobs(m, p):
    """Interest management: only send a player the enemies close to them (saves their CPU and bandwidth)."""
    keep = [a for a in m.get('m', []) if len(a) > 3 and abs(a[2] - p.x) <= MOB_NEAR and abs(a[3] - p.z) <= MOB_NEAR]
    if not keep: return None
    out = {'t': 'mb', 'from': m['from'], 'm': keep}
    if m.get('bx'):
        ids = {a[0] for a in keep}; out['bx'] = [b for b in m['bx'] if b[0] in ids]
    return out


def pick_weather():
    tot = sum(w for _, w in WEATHER); r = random.random() * tot
    for name, w in WEATHER:
        r -= w
        if r <= 0: return name
    return 'clear'


async def handle_ws(reader, writer, headers):
    key = headers.get('sec-websocket-key', '')
    acc = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
    writer.write(('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: %s\r\n\r\n' % acc).encode())
    await writer.drain()
    me = Player(W.next_id, writer); W.next_id += 1
    try:
        while True:
            op, data = await ws_read(reader)
            if op == 0x8: break
            if op == 0x9: writer.write(ws_frame(data, 0xA)); continue
            if op == 0xA: continue
            try: m = json.loads(data.decode())
            except Exception: continue
            t = m.get('t')
            if not me.ok:
                if t != 'hello': continue
                if W.args.password and m.get('pw', '') != W.args.password:
                    send(me, {'t': 'deny', 'why': 'Wrong password.'}); break
                if len(W.players) >= W.args.max:
                    send(me, {'t': 'deny', 'why': 'The realm is full (%d players).' % W.args.max}); break
                me.name = (str(m.get('name', 'Wizard')).strip()[:16] or 'Wizard'); me.ok = True; W.players[me.id] = me
                send(me, {'t': 'welcome', 'id': me.id, 'name': me.name, 'T': W.clock(), 'w': W.weather, 'flags': W.flags,
                          'players': [{'id': p.id, 'name': p.name, 'look': p.look, 'x': p.x, 'z': p.z} for p in W.players.values() if p is not me and p.ok]})
                broadcast({'t': 'join', 'id': me.id, 'name': me.name, 'look': me.look}, skip=me)
                print('[+] %s joined (%d online)' % (me.name, len(W.players)), flush=True)
                continue
            if t == 'p':
                old_inst = me.inst
                try: me.x = float(m.get('x', me.x)); me.z = float(m.get('z', me.z))
                except (TypeError, ValueError): pass
                me.inst = str(m.get('i') or '')[:40]
                if me.inst != old_inst:
                    inst_left(me, old_inst)
            elif t == 'look': me.look = m.get('look') or {}
            if t in RELAY:
                m['from'] = me.id
                if t == 'chat':
                    m['name'] = me.name; m['text'] = str(m.get('text', ''))[:200]; print('<%s> %s' % (me.name, m['text']), flush=True)
                if t == 'flag':
                    W.flags[str(m.get('k'))] = m.get('v')
                if t == 'mb':
                    for p in list(W.players.values()):
                        if p.ok and p is not me:
                            f = filter_mobs(m, p)
                            if f: send(p, f, True)
                    continue
                near = (me.x, me.z) if t in SPATIAL and 'x' in m and 'z' in m else None
                if near: near = (float(m['x']), float(m['z']))
                broadcast(m, skip=me, near=near, droppable=t in ('p', 'mb'))
            elif t in DIRECT:
                m['from'] = me.id; to = W.players.get(m.get('to'))
                if to and to.ok: send(to, m)
            elif t == 'claim':
                k = str(m.get('k')); o = W.claims.get(k)
                if o is None or o == me.id or o not in W.players:
                    W.claims[k] = me.id; send(me, {'t': 'claimed', 'k': k, 'ok': True, 'o': me.id})
                else:
                    send(me, {'t': 'claimed', 'k': k, 'ok': False, 'o': o})
            elif t == 'release':
                k = str(m.get('k'))
                if W.claims.get(k) == me.id:
                    del W.claims[k]; broadcast({'t': 'rel', 'k': k}, skip=me)
    except (asyncio.IncompleteReadError, ConnectionError, OSError):
        pass
    finally:
        if me.ok and me.id in W.players:
            del W.players[me.id]
            inst_left(me, me.inst)
            for k in [k for k, o in W.claims.items() if o == me.id]:
                del W.claims[k]; broadcast({'t': 'rel', 'k': k})
            broadcast({'t': 'leave', 'id': me.id})
            print('[-] %s left (%d online)' % (me.name, len(W.players)), flush=True)
        try: writer.close()
        except Exception: pass


def http_response(code, body=b'', ctype='text/plain', extra=''):
    reason = {200: 'OK', 400: 'Bad Request', 403: 'Forbidden', 404: 'Not Found', 405: 'Method Not Allowed'}.get(code, 'OK')
    return ('HTTP/1.1 %d %s\r\nContent-Type: %s\r\nContent-Length: %d\r\nCache-Control: no-cache\r\nConnection: close\r\n%s\r\n' % (code, reason, ctype, len(body), extra)).encode() + body


async def handle(reader, writer):
    try:
        raw = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 15)
        lines = raw.decode('latin-1').split('\r\n'); req = lines[0].split(' ')
        headers = {}
        for ln in lines[1:]:
            if ':' in ln: k, v = ln.split(':', 1); headers[k.strip().lower()] = v.strip()
        if len(req) < 2: return
        path = urllib.parse.unquote(req[1].split('?', 1)[0])
        if headers.get('upgrade', '').lower() == 'websocket' and path.startswith('/ws'):
            await handle_ws(reader, writer, headers); return
        if req[0] not in ('GET', 'HEAD'):
            writer.write(http_response(405)); await writer.drain(); return
        if path == '/': path = '/index.html'
        full = os.path.normpath(os.path.join(ROOT, path.lstrip('/')))
        if not full.startswith(ROOT + os.sep):
            writer.write(http_response(403, b'forbidden')); await writer.drain(); return
        if not os.path.isfile(full):
            writer.write(http_response(404, b'not found')); await writer.drain(); return
        with open(full, 'rb') as f: body = f.read()
        ctype = mimetypes.guess_type(full)[0] or 'application/octet-stream'
        writer.write(http_response(200, b'' if req[0] == 'HEAD' else body, ctype)); await writer.drain()
    except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError, OSError, asyncio.LimitOverrunError):
        pass
    finally:
        try: writer.close()
        except Exception: pass


async def ticker():
    wx_at = time.time() + random.uniform(100, 280)
    while True:
        await asyncio.sleep(5)
        broadcast({'t': 'clock', 'T': W.clock()})
        if int(time.time()) % 30 < 5: W.save()
        if time.time() > wx_at:
            wx_at = time.time() + random.uniform(100, 280); W.weather = pick_weather(); broadcast({'t': 'wx', 'w': W.weather})


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1)); return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


async def amain(args):
    global W
    W = World(args); host = '0.0.0.0' if args.lan else '127.0.0.1'; srv = None; port = args.port
    for port in range(args.port, args.port + 20):
        try:
            srv = await asyncio.start_server(handle, host, port, limit=1 << 17); break
        except OSError:
            continue
    if srv is None: sys.exit('Could not find a free port near %d' % args.port)
    url = 'http://localhost:%d/' % port
    print('Emberwild Realms is running at ' + url)
    v = installed_version()
    if v.get('short'): print('Game version: %s%s' % (v['short'], ' (%s)' % v['date'] if v.get('date') else ''))
    if args.lan:
        ip = lan_ip()
        print('CO-OP: friends on your network open  http://%s:%d/  and press "Co-op multiplayer" on the title screen.' % (ip or '<your-ip>', port))
        print('       (Over the internet: forward port %d on your router, or use a tunnel such as ngrok / cloudflared.)' % port)
        if args.password: print('       Room password is set.')
    else:
        print('Solo mode. Run with --lan to let friends join.')
    print('Press Ctrl+C to stop.', flush=True)
    if not args.no_browser: threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    asyncio.create_task(ticker())
    async with srv: await srv.serve_forever()


def main():
    ap = argparse.ArgumentParser(description='Run Emberwild Realms locally (and host co-op)')
    ap.add_argument('--port', type=int, default=8000)
    ap.add_argument('--lan', '--coop', dest='lan', action='store_true', help='listen on all network interfaces so friends can join')
    ap.add_argument('--password', default='', help='require this password to join co-op')
    ap.add_argument('--max', type=int, default=8, help='maximum co-op players')
    ap.add_argument('--save', default='world_state.json', help='file (next to play.py) where the shared world is kept between runs')
    ap.add_argument('--fresh', action='store_true', help='start a brand-new shared world and do not save it')
    ap.add_argument('--no-update', action='store_true', help='do not check GitHub for a newer version of the game')
    ap.add_argument('--check', action='store_true', help='only check for / install updates, then exit')
    ap.add_argument('--force-update', action='store_true', help='re-verify every file against GitHub even if the version matches')
    ap.add_argument('--repo', default='', help='GitHub repo to update from, as owner/name (default: %s)' % REPO)
    ap.add_argument('--branch', default='', help='branch or tag to follow (default: the repo default branch)')
    ap.add_argument('--token', default='', help='GitHub token for a private repo (or set GITHUB_TOKEN)')
    ap.add_argument('--no-browser', action='store_true')
    args = ap.parse_args()
    res = check_for_update(args)
    if res == 'updated-self':
        print('The launcher itself was updated - restarting...', flush=True)
        env = dict(os.environ); env['EW_UPDATED'] = '1'
        sys.exit(subprocess.call([sys.executable, os.path.join(ROOT, 'play.py')] + sys.argv[1:], env=env))
    if args.check: return
    if not os.path.exists(os.path.join(ROOT, 'index.html')): sys.exit('index.html not found next to play.py')
    try: asyncio.run(amain(args))
    except KeyboardInterrupt: print('\nStopped.')
    finally:
        if W: W.save()


if __name__ == '__main__':
    main()
