# -*- coding: utf-8 -*-
"""Trae 网页登录取凭据（不需要客户端）。

用官网 OAuth 回环换 refreshToken，设备身份在本地生成（16 位设备号 + P-256 密钥对），
所以不用安装或启动客户端。只有输手机验证码那一步需要人。
"""
import base64
import hashlib
import http.server
import json
import os
import secrets
import socket
import sys
import time
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
DEVICE_FILE = os.path.join(HERE, '.trae_oauth_devices.json')
LINE = 'trae'                 # IDE 线；SOLO 线换成 'solo'
PREFERRED_PORT = 17388        # 官网优先探测这个端口；占了就换随机端口（回调 URI 会带上真实端口）

for _p in (HERE,):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import trae_checkin as CK       # P-256 曲线常数与点运算（纯标准库）
import trae_sms_login as SL     # 已验证的 PKCE / 授权 URL / 兑换链 / 回调解析


def _log(log, text, level='dim'):
    (log or (lambda m, lv='dim': print(m, flush=True)))(text, level)


# DER / PEM（PKCS#8 与 SPKI）
# 之所以手写：本机没有 cryptography，而客户端只认标准 PKCS#8 PEM，
# 现有 trae_checkin 已有 P-256 点运算，缺的只是「导出」这一半。
OID_EC_PUBLIC_KEY = bytes.fromhex('2a8648ce3d0201')     # 1.2.840.10045.2.1
OID_PRIME256V1 = bytes.fromhex('2a8648ce3d030107')      # 1.2.840.10045.3.1.7


def _der_len(n):
    if n < 0x80:
        return bytes([n])
    body = n.to_bytes((n.bit_length() + 7) // 8, 'big')
    return bytes([0x80 | len(body)]) + body


def _der(tag, body):
    return bytes([tag]) + _der_len(len(body)) + body


def _pem(label, der):
    text = base64.b64encode(der).decode('ascii')
    lines = [text[i:i + 64] for i in range(0, len(text), 64)]
    return '-----BEGIN %s-----\n%s\n-----END %s-----' % (label, '\n'.join(lines), label)


def gen_keypair():
    """生成 P-256 密钥对，返回 (私钥 PEM/PCKS#8, 公钥 PEM/SPKI, 私钥标量, 公钥点)。"""
    scalar = secrets.randbelow(CK.P256_N - 1) + 1
    point = CK._ec_mul(scalar, (CK.P256_GX, CK.P256_GY))
    x, y = point
    uncompressed = b'\x04' + x.to_bytes(32, 'big') + y.to_bytes(32, 'big')
    alg = _der(0x30, _der(0x06, OID_EC_PUBLIC_KEY) + _der(0x06, OID_PRIME256V1))
    ec_private = _der(0x30,
                      _der(0x02, b'\x01') +                       # version 1
                      _der(0x04, scalar.to_bytes(32, 'big')) +    # privateKey
                      _der(0xA1, _der(0x03, b'\x00' + uncompressed)))   # [1] publicKey
    pkcs8 = _der(0x30, _der(0x02, b'\x00') + alg + _der(0x04, ec_private))
    spki = _der(0x30, alg + _der(0x03, b'\x00' + uncompressed))
    return _pem('PRIVATE KEY', pkcs8), _pem('PUBLIC KEY', spki), scalar, point


def pem_public_point(pem):
    """从 SPKI PEM 里取出公钥点（自检用）。"""
    der = base64.b64decode(''.join(l for l in pem.splitlines() if '---' not in l))
    tail = der[-65:]
    if tail[0] != 0x04:
        raise ValueError('公钥格式异常')
    return (int.from_bytes(tail[1:33], 'big'), int.from_bytes(tail[33:], 'big'))


def verify_signature(pub_pem, data, signature_b64):
    """纯 Python ECDSA 校验（只用于离线自检，确认导出的密钥能被正常签名/验签）。"""
    der = base64.b64decode(signature_b64)
    i = 2
    if der[1] & 0x80:
        i = 2 + (der[1] & 0x7f)
    r_len = der[i + 1]
    r = int.from_bytes(der[i + 2:i + 2 + r_len], 'big')
    j = i + 2 + r_len
    s_len = der[j + 1]
    s = int.from_bytes(der[j + 2:j + 2 + s_len], 'big')
    z = int.from_bytes(hashlib.sha256(data).digest(), 'big')
    w = pow(s, -1, CK.P256_N)
    u1 = (z * w) % CK.P256_N
    u2 = (r * w) % CK.P256_N
    point = CK._ec_add(CK._ec_mul(u1, (CK.P256_GX, CK.P256_GY)),
                       CK._ec_mul(u2, pem_public_point(pub_pem)))
    return point is not None and point[0] % CK.P256_N == r


# 设备身份（持久化）
def _read_devices():
    try:
        with open(DEVICE_FILE, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_devices(data):
    tmp = DEVICE_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, DEVICE_FILE)
    try:
        os.chmod(DEVICE_FILE, 0o600)
    except Exception:
        pass


def gen_device():
    """造一套全新设备身份：16 位数字设备号 + 32 位 hex machineId + P-256 密钥对。"""
    private_pem, public_pem, scalar, _ = gen_keypair()
    return {
        'device_id': ''.join(secrets.choice('0123456789') for _ in range(16)),
        'machine_id': secrets.token_hex(16),
        'private_key': private_pem,
        'public_key': public_pem,
        'scalar': scalar,
        'created_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'source': 'self',
    }


def load_device(key='default', create=True):
    """取（或新造）某个 key 对应的设备身份。同一账号必须始终用同一套。"""
    key = str(key or 'default').strip() or 'default'
    devices = _read_devices()
    dev = devices.get(key)
    if isinstance(dev, dict) and dev.get('private_key') and dev.get('public_key'):
        return dev
    if not create:
        return None
    dev = gen_device()
    devices[key] = dev
    _write_devices(devices)
    return dev

def list_devices():
    return [{'key': k, 'device_id': (v or {}).get('device_id', ''),
             'created_at': (v or {}).get('created_at', '')}
            for k, v in sorted(_read_devices().items())]

def as_sl_device(dev):
    """转换成 trae_sms_login 期待的形状（snake_case、public_key 必填）。"""
    return {'device_id': dev['device_id'], 'public_key': dev['public_key'],
            'machine_id': dev.get('machine_id', ''), 'from': 'self'}


# 授权 URL 与回调服务
def pick_port(preferred=PREFERRED_PORT):
    """优先 17388（官网更认），被占就用系统分配的随机端口。"""
    for port in (preferred, 0):
        if port is None:
            continue
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(('127.0.0.1', port))
                return sock.getsockname()[1]
        except OSError:
            continue
    return 0


def build_url(dev, port, trace_id, challenge):
    return SL.build_authorize_url(LINE, as_sl_device(dev), SL.system_facts(),
                                  port, trace_id, challenge)


def serve_callback(port, timeout, on_params, stop=None):
    """在 127.0.0.1:port 上等回调。on_params(params) 返回 True 表示可以收工了。"""
    state = {'done': False}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body, ctype):
            raw = body.encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', '*')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            if raw:
                self.wfile.write(raw)

        def do_OPTIONS(self):
            self._send(204, '', 'text/plain; charset=utf-8')

        def do_HEAD(self):
            self._send(200, '', 'text/plain; charset=utf-8')

        def do_GET(self):
            self._handle()

        def do_POST(self):
            self._handle()

        def _handle(self):
            query = self.path.split('?', 1)[1] if '?' in self.path else ''
            params = SL.parse_query(query)
            if not SL.has_credential_marker(params):
                # 官网会先探测端口是否活着，回一句 ok 就行
                self._send(200, 'ok', 'text/plain; charset=utf-8')
                return
            ok, title, lines = on_params(params)
            self._send(200, SL._html_page(ok, title, lines), 'text/html; charset=utf-8')
            if ok:
                state['done'] = True

    try:
        httpd = http.server.HTTPServer(('127.0.0.1', port), Handler)
    except OSError as exc:
        return '端口 %d 无法监听：%s' % (port, exc)
    httpd.timeout = 0.8
    deadline = time.monotonic() + timeout
    try:
        while not state['done'] and time.monotonic() < deadline:
            if stop is not None and getattr(stop, 'is_set', lambda: False)():
                return 'CANCELLED'
            httpd.handle_request()
    finally:
        httpd.server_close()
    return None if state['done'] else 'TIMEOUT'


# 完整登录
def web_login(name='', device_key='', timeout=300, open_browser=True, log=None,
              stop=None, host='', on_url=None):
    """走完「授权 URL → 浏览器登录 → 回调 → 兑换」，返回可直接入库的账号。

    on_url 会在生成授权链接后回调一次，方便界面提供「重新打开浏览器」。
    返回 {'ok','account','error','device_key','waited'}
    """
    device_key = str(device_key or '').strip() or (name or 'default')
    dev = load_device(device_key)
    port = pick_port()
    trace_id = secrets.token_hex(16)
    verifier, challenge = SL.pkce_pair()
    url = build_url(dev, port, trace_id, challenge)
    if on_url:
        try:
            on_url(url)
        except Exception:
            pass

    _log(log, '设备身份：%s（machineId %s…，自造密钥）'
         % (dev['device_id'], dev.get('machine_id', '')[:8]), 'dim')
    _log(log, '回调地址：http://127.0.0.1:%d/authorize' % port, 'dim')
    _log(log, '请在浏览器里用「%s」的手机号登录并输入验证码（%d 秒内有效）…'
         % (name or '待登录账号', timeout), 'head')
    _log(log, '授权链接：%s' % url, 'dim')
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception as exc:
            _log(log, '自动打开浏览器失败（%s），请手动复制上面的链接' % exc, 'warn')

    outcome = {'info': None, 'error': None}
    started = time.time()

    def on_params(params):
        try:
            info = SL.parse_callback(params)
        except Exception as exc:
            outcome['error'] = '回调格式异常：%s' % exc
            return False, '登录失败', [str(exc)]
        got_trace = str(info.get('trace') or '').strip()
        if got_trace and got_trace != trace_id:
            outcome['error'] = '回调 trace 与本机发起的请求不匹配（可能被重放）'
            return False, '校验失败', ['登录请求校验不通过，请重新发起登录']
        if not (info.get('auth_code') or info.get('refresh_token')):
            outcome['error'] = '回调里没有 AuthCode / refreshToken'
            return False, '登录失败', ['没有拿到授权码，请重新登录']
        outcome['info'] = info
        return True, '登录成功', ['授权已完成，可以关闭这个页面了',
                                  '正在把账号写入面板…']

    error = serve_callback(port, timeout, on_params, stop=stop)
    if error == 'CANCELLED':
        return {'ok': False, 'error': '已取消', 'account': None, 'device_key': device_key,
                'waited': time.time() - started}
    if outcome['error']:
        return {'ok': False, 'error': outcome['error'], 'account': None,
                'device_key': device_key, 'waited': time.time() - started}
    if error == 'TIMEOUT' or not outcome['info']:
        return {'ok': False, 'error': '等待登录超时（%d 秒）' % int(timeout), 'account': None,
                'device_key': device_key, 'waited': time.time() - started}

    info = outcome['info']
    access = refresh = ''
    if info.get('refresh_token'):
        refresh = info['refresh_token']
    else:
        _log(log, '已收到授权码，正在兑换 token…', 'dim')
        tokens, err = SL.exchange_auth_code(LINE, info.get('host') or host,
                                            info['auth_code'], verifier,
                                            as_sl_device(dev), SL.system_facts())
        if not tokens:
            return {'ok': False, 'error': '兑换 token 失败：%s' % err, 'account': None,
                    'device_key': device_key, 'waited': time.time() - started}
        access = tokens.get('accessToken') or ''
        refresh = tokens.get('refreshToken') or ''

    uid = str(info.get('uid') or '').strip() or SL.jwt_uid(access)
    real_name = str(info.get('name') or '').strip()
    account = {
        # name 是面板里的备注名；username 是账号真名（写回客户端时用真的，否则 IDE 会显示备注）
        'name': str(name or real_name or uid or '网页登录账号').strip(),
        'username': real_name,
        'uid': uid,
        'refreshToken': refresh,
        'accessToken': access,
        'deviceId': dev['device_id'],
        'realDeviceId': dev['device_id'],
        'deviceKeyPem': dev['private_key'],
        'devicePubPem': dev['public_key'],
        'machineId': dev.get('machine_id', ''),
        'oauthDevice': device_key,
        'source': '网页登录（自造设备身份）',
        'enabled': True,
    }
    if not refresh:
        return {'ok': False, 'error': '没拿到 refreshToken（该账号无法续期）', 'account': None,
                'device_key': device_key, 'waited': time.time() - started}
    _log(log, '✅ 登录成功：%s（UID %s，设备号 %s）'
         % (account['name'], uid or '-', dev['device_id']), 'ok')
    return {'ok': True, 'account': account, 'error': '', 'device_key': device_key,
            'waited': time.time() - started}


def run_batch(names, timeout=300, open_browser=True, log=None, stop=None, on_result=None):
    """依次登录多个账号（每个账号一套独立设备身份）。"""
    results = []
    total = len(names)
    for index, name in enumerate(names, 1):
        if stop is not None and getattr(stop, 'is_set', lambda: False)():
            _log(log, '已中止，剩余 %d 个未登录' % (total - index + 1), 'warn')
            break
        _log(log, '── 第 %d/%d 个：%s ──' % (index, total, name), 'head')
        res = web_login(name=name, timeout=timeout, open_browser=open_browser,
                        log=log, stop=stop)
        res['name'] = name
        results.append(res)
        if on_result:
            try:
                on_result(name, res)
            except Exception as exc:
                _log(log, '回调处理异常：%s' % exc, 'err')
    ok = sum(1 for r in results if r.get('ok'))
    return {'ok': ok, 'fail': len(results) - ok, 'total': total, 'results': results}


# 自检 / 命令行
def selftest():
    """离线自检：密钥导出 → 签名 → 验签 → 设备号格式，全程不联网。"""
    private_pem, public_pem, scalar, point = gen_keypair()
    assert CK.ec_private_scalar(private_pem) == scalar, '私钥导出后解析不一致'
    assert pem_public_point(public_pem) == point, '公钥导出后解析不一致'
    payload = b'GET\n/trae/api/v3/oauth/ExchangeToken\nselftest'
    sig = CK.ec_sign(private_pem, payload)
    assert verify_signature(public_pem, payload, sig), '签名验签失败'
    assert not verify_signature(public_pem, payload + b'x', sig), '验签应失败却通过了'
    dev = gen_device()
    assert len(dev['device_id']) == 16 and dev['device_id'].isdigit()
    assert len(dev['machine_id']) == 32
    port = pick_port()
    assert port > 0
    url = build_url(dev, port, 'trace', 'challenge')
    # 回调地址是 URL 编码过的（http%3A%2F%2F127.0.0.1%3A端口），所以只校验端口号出现
    assert 'code_challenge_method=S256' in url and str(port) in url
    assert dev['device_id'] in url and 'auth_from=trae' in url
    return 'selftest ok: 密钥 %d 位标量, 设备号 %s, 端口 %d' % (
        scalar.bit_length(), dev['device_id'], port)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if '--selftest' in argv:
        print(selftest())
        return 0
    if '--devices' in argv:
        for item in list_devices():
            print('%-20s %s  (创建于 %s)' % (item['key'], item['device_id'], item['created_at']))
        return 0
    if '--login' in argv:
        idx = argv.index('--login')
        name = argv[idx + 1] if idx + 1 < len(argv) and not argv[idx + 1].startswith('-') else '账号'
        timeout = 300
        if '--timeout' in argv:
            try:
                timeout = int(argv[argv.index('--timeout') + 1])
            except Exception:
                pass
        res = web_login(name=name, timeout=timeout,
                        open_browser='--no-open' not in argv)
        if res.get('ok'):
            acc = res['account']
            print('=' * 62)
            print('账号：%s  UID %s' % (acc['name'], acc['uid']))
            print('设备号：%s' % acc['realDeviceId'])
            print('refreshToken：%s' % acc['refreshToken'])
            print('=' * 62)
            print('可直接把上面这段交给面板「账号列表 → 添加账号」或写进 TRAE_ACCOUNTS。')
            return 0
        print('[X] %s' % res.get('error'))
        return 1
    print(__doc__)
    print('用法：')
    print('  python trae_weblogin.py --selftest        离线自检（不联网）')
    print('  python trae_weblogin.py --devices         列出已造的设备身份')
    print('  python trae_weblogin.py --login 名字      发起一次网页登录')
    return 0


if __name__ == '__main__':
    sys.exit(main())
