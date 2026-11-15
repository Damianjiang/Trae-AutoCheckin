# -*- coding: utf-8 -*-
"""把面板里的账号写进客户端（冷切换），失败自动回滚。

流程：结束客户端 → 备份登录态 → 重写 storage.json 的身份三键 → 拉起 → 核对 uid。
"""
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import trae_km as KM
import trae_autologin as AL          # child_env / _ps / _pid_alive / find_clients

KEY_AUTH = 'iCubeAuthInfo://icube.cloudide'
KEY_TAG = 'iCubeAuthInfo://usertag'
KEY_SERVER = 'iCubeServerData://icube.cloudide'
PREFIX_DC = 'iCubeAuthInfo://icube-dc:'
STORAGE_REL = 'User/globalStorage/storage.json'
HOST = 'https://api.trae.cn'
BACKUP_ROOT = os.path.join(HERE, '.trae_switch_backups')

# 扫描载体时跳过的目录（体积大且与账号身份无关）
EXCLUDE_DIRS = {d.lower() for d in (
    'Cache', 'CachedData', 'CachedConfigurations', 'CachedProfilesData',
    'CachedExtensionVSIXs', 'Code Cache', 'GPUCache', 'Crashpad',
    'DawnGraphiteCache', 'DawnWebGPUCache', 'ShaderCache', 'Dictionaries',
    'blob_storage', 'Service Worker', 'Shared Dictionary', 'logs', 'Backups',
    'ModularData', 'node_modules')}
# 分区本地状态：不是登录态，切换时绝不能替换
GLOBAL_STATE_MARKER = 'local storage/leveldb'


def _log(log, text, level='dim'):
    (log or (lambda m, lv='dim': print(m, flush=True)))(text, level)


# 客户端目录定位
def user_data_dir(product=''):
    """客户端用户数据目录（%APPDATA%\\Trae CN 之类）。"""
    appdata = os.environ.get('APPDATA') or os.path.expanduser('~/AppData/Roaming')
    names = [product] if product else ['Trae CN', 'TRAE SOLO CN', 'TRAE SOLO', 'Trae']
    for name in names:
        path = os.path.join(appdata, name)
        if os.path.isfile(os.path.join(path, 'User', 'globalStorage', 'storage.json')):
            return path
    return os.path.join(appdata, names[0])


def known_clients():
    """本机可绑定的客户端（有 storage.json 的才算）。"""
    appdata = os.environ.get('APPDATA') or os.path.expanduser('~/AppData/Roaming')
    out = []
    for name in ('Trae CN', 'TRAE SOLO CN', 'TRAE SOLO', 'Trae'):
        path = os.path.join(appdata, name)
        if os.path.isfile(os.path.join(path, 'User', 'globalStorage', 'storage.json')):
            out.append(name)
    return out


def resolve_root(account=None, root=None):
    """决定要操作哪个客户端：显式 root > 账号绑定的客户端 > 自动探测。"""
    if root:
        return root
    client = str((account or {}).get('client') or '').strip()
    if client:
        path = os.path.join(os.environ.get('APPDATA') or '', client)
        if os.path.isdir(path):
            return path
    return user_data_dir()


def storage_path(root=None):
    return os.path.join(root or user_data_dir(), 'User', 'globalStorage', 'storage.json')


def client_image_name(root=None):
    """由数据目录反推可执行文件名（用于结束进程）。"""
    name = os.path.basename(root or user_data_dir())
    return name + '.exe'


# 载体条目
def detect_entries(root):
    """登录态载体：storage.json + 深度 ≤5 的所有 leveldb 目录（跳过分区本地状态）。"""
    found = []
    if os.path.isfile(os.path.join(root, 'User', 'globalStorage', 'storage.json')):
        found.append(STORAGE_REL)

    def walk(rel, depth):
        if depth > 5 or len(found) >= 16:
            return
        path = os.path.join(root, *rel.split('/')) if rel else root
        try:
            children = list(os.scandir(path))
        except OSError:
            return
        for child in children:
            if not child.is_dir():
                continue
            child_rel = ('%s/%s' % (rel, child.name)) if rel else child.name
            lower = child_rel.lower()
            if child.name.lower() == 'leveldb':
                if GLOBAL_STATE_MARKER not in lower:
                    found.append(child_rel)
            elif child.name.lower() not in EXCLUDE_DIRS:
                walk(child_rel, depth + 1)

    walk('', 0)
    return found


def expand_entries(root, entries):
    """目录条目展开成具体文件（目录则递归其中全部文件）。"""
    out = []
    for rel in entries:
        path = os.path.join(root, *rel.split('/'))
        if os.path.isdir(path):
            for base, _dirs, files in os.walk(path):
                for name in files:
                    full = os.path.join(base, name)
                    out.append(os.path.relpath(full, root).replace('\\', '/'))
        elif os.path.isfile(path):
            out.append(rel)
    return out


def _copy_entry(src_root, dst_root, rel):
    src = os.path.join(src_root, *rel.split('/'))
    dst = os.path.join(dst_root, *rel.split('/'))
    if not os.path.exists(src):
        return False
    if os.path.isdir(src):
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(src, dst, dirs_exist_ok=True)
    else:
        os.makedirs(os.path.dirname(dst) or '.', exist_ok=True)
        shutil.copy2(src, dst)
    return True


def backup_carrier(root, entries, log=None):
    """把当前载体整组备份出来，返回备份目录。只保留最近 10 份。"""
    stamp = time.strftime('%Y%m%d-%H%M%S')
    dest = os.path.join(BACKUP_ROOT, stamp)
    os.makedirs(dest, exist_ok=True)
    count = 0
    for rel in entries:
        if _copy_entry(root, dest, rel):
            count += 1
    _log(log, '已备份当前登录态（%d 项）→ %s' % (count, dest), 'dim')
    _prune_backups(keep=10)
    return dest


def _prune_backups(keep=10):
    """备份只留最近 N 份，避免目录无限膨胀。"""
    try:
        names = sorted((n for n in os.listdir(BACKUP_ROOT)
                        if os.path.isdir(os.path.join(BACKUP_ROOT, n))), reverse=True)
        for name in names[keep:]:
            shutil.rmtree(os.path.join(BACKUP_ROOT, name), ignore_errors=True)
    except Exception:
        pass


def _token_expired(token):
    """accessToken 的 exp 是否已过（解不出来按未过期处理，交给客户端自己续期）。"""
    try:
        import base64
        seg = token.split('.')[1]
        seg += '=' * (-len(seg) % 4)
        exp = int(json.loads(base64.urlsafe_b64decode(seg)).get('exp') or 0)
        return bool(exp) and exp < time.time() + 60
    except Exception:
        return False


def restore_carrier(src_root, root, entries, log=None):
    count = 0
    for rel in entries:
        if _copy_entry(src_root, root, rel):
            count += 1
    _log(log, '已还原 %d 项登录态' % count, 'dim')
    return count


# 读当前登录态
def read_storage(root=None):
    path = storage_path(root)
    with open(path, 'r', encoding='utf-8') as fh:
        data = json.load(fh)
    return data if isinstance(data, dict) else {}


def auto_bind(account):
    """推断账号该绑哪个客户端：优先「当前就登着这个号」的，其次同设备号能取到密钥的。"""
    account = dict(account or {})
    uid = str(account.get('uid') or '').strip()
    candidates = []
    for client in known_clients():
        info = bind_info(dict(account, client=client))
        if info.get('hasKeys'):
            candidates.append((0 if info.get('isCurrent') else 1, info))
    if not candidates:
        return {}
    candidates.sort(key=lambda item: item[0])
    info = candidates[0][1]
    return {'client': info['client'], 'deviceKeyPem': info['privateKeyPem'],
            'devicePubPem': info['publicKeyPem'], 'machineId': info['machineId'],
            'realDeviceId': info['deviceId'] or account.get('realDeviceId')}


def describe_live(root=None):
    """当前客户端登录的账号：uid / 昵称 / 设备号 / 是否有 refreshToken。"""
    try:
        storage = read_storage(root)
    except Exception as exc:
        return {'loggedIn': False, 'error': str(exc)}
    value = storage.get(KEY_AUTH)
    info = {'loggedIn': False, 'uid': '', 'username': '', 'deviceId': '',
            'hasRefresh': False, 'path': storage_path(root)}
    if not value:
        return info
    try:
        creds = KM.decode_json(value)
    except Exception as exc:
        info['error'] = '凭据解密失败：%s' % exc
        return info
    account = creds.get('account') or {}
    device = ''
    for key in storage:
        if str(key).startswith(PREFIX_DC):
            did = str(key)[len(PREFIX_DC):]
            if did.startswith('aha-') or (did.isdigit() and 12 <= len(did) <= 20):
                device = device or did
    info.update({'loggedIn': bool(creds.get('refreshToken')),
                 'uid': str(creds.get('userId') or ''),
                 'username': str(account.get('username') or ''),
                 'phone': str(account.get('nonPlainTextMobile') or ''),
                 'deviceId': device,
                 'hasRefresh': bool(creds.get('refreshToken')),
                 'expiredAt': str(creds.get('expiredAt') or '')})
    return info


def device_pair(root=None, device_id=''):
    """从客户端 storage.json 取某设备号对应的密钥对（老账号补密钥时用）。"""
    try:
        storage = read_storage(root)
    except Exception:
        return {}
    did = str(device_id or '').strip()
    if not did:
        for key in storage:
            if str(key).startswith(PREFIX_DC):
                did = str(key)[len(PREFIX_DC):]
                break
    raw = storage.get(PREFIX_DC + did)
    if not raw:
        return {}
    try:
        pair = KM.decode_json(raw)
    except Exception:
        return {}
    return {'deviceId': did,
            'privateKeyPem': str(pair.get('privateKeyPEM') or '').strip(),
            'publicKeyPem': str(pair.get('publicKeyPEM') or '').strip(),
            'machineId': str(storage.get('telemetry.machineId') or '').strip()}


def bind_info(account, root=None):
    """账号在本机的绑定情况：绑到哪个客户端、有没有设备密钥、当前是否就登着它。"""
    account = dict(account or {})
    root = resolve_root(account, root)
    product = os.path.basename(root.rstrip('\\/'))
    uid = str(account.get('uid') or '').strip()
    did = str(account.get('realDeviceId') or account.get('deviceId') or '').strip()
    live = describe_live(root)
    pair = device_pair(root, did) if did else {}
    if not pair:
        pair = device_pair(root)
    return {
        'client': product,
        'deviceId': did or pair.get('deviceId', ''),
        'hasKeys': bool(account.get('deviceKeyPem') and account.get('devicePubPem'))
                   or bool(pair.get('privateKeyPem')),
        'privateKeyPem': pair.get('privateKeyPem', ''),
        'publicKeyPem': pair.get('publicKeyPem', ''),
        'machineId': pair.get('machineId', ''),
        'liveUid': str(live.get('uid') or ''),
        'liveUser': str(live.get('username') or ''),
        'isCurrent': bool(uid) and str(live.get('uid') or '') == uid,
    }


# 合成目标登录态
def _iso_now():
    return time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime())


def _jwt_exp_iso(token):
    """从 accessToken 里取 exp，转成客户端那种 RFC3339 文本。"""
    try:
        import base64
        seg = token.split('.')[1]
        seg += '=' * (-len(seg) % 4)
        payload = json.loads(base64.urlsafe_b64decode(seg))
        return time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime(int(payload['exp'])))
    except Exception:
        return _iso_now()


def build_auth_record(account, donor=None):
    """拼出 `iCubeAuthInfo://icube.cloudide` 的明文结构。

    donor 是当前登录态的 account 体：以它为底，只覆盖身份字段，其余留空，
    这样不同客户端版本多出来的字段不会缺。
    """
    donor = donor if isinstance(donor, dict) else {}
    body = dict(donor)
    for key in ('email', 'organization', 'nonPlainTextMobile', 'description',
                'work_country', 'iss'):
        body[key] = ''
    body.update({
        'username': str(account.get('username') or account.get('name') or ''),
        'avatar_url': str(account.get('avatar') or ''),
        'userTag': 'cn',
        'iat': 0,
        'migrateToSG': False,
    })
    body.setdefault('storeRegion', 'CN')
    body.setdefault('scope', 'marscode')
    body.setdefault('loginScope', 'trae')
    body.setdefault('storeCountryCode', '')
    body.setdefault('storeCountrySrc', '')
    access = str(account.get('accessToken') or '')
    return {
        'token': access,
        'refreshToken': str(account.get('refreshToken') or ''),
        'expiredAt': _jwt_exp_iso(access) if access else _iso_now(),
        'refreshExpiredAt': None,
        'tokenReleaseAt': _iso_now(),
        'userId': str(account.get('uid') or ''),
        'host': HOST,
        'userRegion': {'region': 'CN', '_aiRegion': 'CN'},
        'account': body,
    }


def synth_storage(storage, account):
    """以现有 storage 为骨架，重写身份三键，返回新的 storage 字典。"""
    storage = dict(storage or {})
    device_id = str(account.get('realDeviceId') or account.get('deviceId') or '').strip()
    priv = str(account.get('deviceKeyPem') or account.get('privateKeyPem') or '').strip()
    pub = str(account.get('devicePubPem') or account.get('publicKeyPem') or '').strip()
    if not device_id:
        raise ValueError('账号缺少设备号，无法写入客户端（请先重新登录该账号）')
    if not (priv and pub):
        # 老账号可能只存了 refreshToken；它绑定的设备密钥通常还在客户端里，按设备号取回
        donor_key = storage.get(PREFIX_DC + device_id)
        if donor_key:
            try:
                pair = KM.decode_json(donor_key)
                priv = str(pair.get('privateKeyPEM') or '').strip()
                pub = str(pair.get('publicKeyPEM') or '').strip()
            except Exception:
                priv = pub = ''
    if not (priv and pub):
        raise ValueError('账号缺少设备密钥对，客户端将无法续期（请先在面板里重新登录该账号）')

    donor = None
    try:
        if storage.get(KEY_AUTH):
            donor = (KM.decode_json(storage[KEY_AUTH]) or {}).get('account')
    except Exception:
        donor = None

    uid = str(account.get('uid') or '')
    auth = build_auth_record(account, donor)
    storage[KEY_AUTH] = KM.encode_json(auth)

    try:
        tags = KM.decode_json(storage[KEY_TAG]) if storage.get(KEY_TAG) else {}
        if not isinstance(tags, dict):
            tags = {}
    except Exception:
        tags = {}
    if uid:
        tags[uid] = 'cn'
    storage[KEY_TAG] = KM.encode_json(tags)

    storage[PREFIX_DC + device_id] = KM.encode_json(
        {'privateKeyPEM': priv, 'publicKeyPEM': pub})
    storage.pop(KEY_SERVER, None)      # 上一个账号的权益快照，必须抹掉
    return storage


# 进程控制
def client_pids(image_name):
    script = ("$ErrorActionPreference='SilentlyContinue'; "
              "Get-Process -Name '%s' | Select-Object -ExpandProperty Id" %
              str(image_name).replace('.exe', '').replace("'", "''"))
    out = AL._ps(script)
    return [int(x) for x in out.split() if x.strip().isdigit()]


def kill_client(image_name, timeout=20, log=None):
    pids = client_pids(image_name)
    if not pids:
        return True
    _log(log, '正在结束 Trae 进程（%d 个）…' % len(pids), 'dim')
    AL._post_wm_close(pids)
    deadline = time.time() + 3
    while time.time() < deadline and client_pids(image_name):
        time.sleep(0.4)
    left = client_pids(image_name)
    if left:
        AL._stop_pids(left)
    deadline = time.time() + max(3, timeout)
    while time.time() < deadline:
        if not client_pids(image_name):
            return True
        time.sleep(0.5)
    return not client_pids(image_name)


def launch_client(exe, log=None):
    import subprocess
    proc = subprocess.Popen([exe], env=AL.child_env(), close_fds=True,
                            cwd=os.path.dirname(exe) or None)
    _log(log, '已启动客户端：%s' % os.path.basename(exe), 'dim')
    return proc


def _find_exe(product_hint=''):
    clients = AL.find_clients()
    if not clients:
        return None
    for item in clients:
        if product_hint and product_hint.lower() in item['label'].lower():
            return item['exe']
    return clients[0]['exe']


# 冷切换
def switch_to(account, root=None, exe=None, relaunch=True, timeout=25, log=None):
    """把客户端切到 account；失败自动回滚。返回 {'ok','outcome','message','uid'}。"""
    root = resolve_root(account, root)
    account = dict(account or {})
    target_uid = str(account.get('uid') or '')
    target_name = str(account.get('name') or target_uid or '目标账号')
    result = {'ok': False, 'outcome': '', 'message': '', 'uid': '', 'backup': '',
              'account': target_name}

    if not account.get('refreshToken'):
        result['message'] = '该账号没有 refreshToken，请先登录'
        return result
    if not os.path.isfile(storage_path(root)):
        result['message'] = '找不到 %s，请先打开一次 Trae 客户端' % storage_path(root)
        return result

    image = client_image_name(root)
    exe = exe or _find_exe(os.path.basename(root))
    entries = detect_entries(root) or [STORAGE_REL]

    # ① 结束客户端
    if not kill_client(image, log=log):
        result['message'] = 'Trae 进程没能退出，请手动关闭后重试'
        return result

    # ② 备份
    try:
        backup = backup_carrier(root, entries, log=log)
        result['backup'] = backup
    except Exception as exc:
        result['message'] = '备份当前登录态失败：%s' % exc
        return result

    if _token_expired(str(account.get('accessToken') or '')):
        _log(log, '提示：该账号的 accessToken 已过期，客户端启动后会用自己的设备密钥自动续期',
             'dim')
    # ③ 写入目标账号
    try:
        storage = read_storage(root)
        storage = synth_storage(storage, account)
        path = storage_path(root)
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(storage, fh, ensure_ascii=False)
        os.replace(tmp, path)
        _log(log, '已写入「%s」的登录态（设备号 %s）'
             % (target_name, account.get('realDeviceId') or account.get('deviceId')), 'dim')
    except Exception as exc:
        restore_carrier(backup, root, entries, log=log)
        result['message'] = '写入失败，已回滚：%s' % exc
        return result

    if not relaunch:
        result.update({'ok': True, 'outcome': 'needs_confirm',
                       'message': '已写入登录态，请手动打开 Trae 验证'})
        return result

    # ④ 拉起 + ⑤ 校验
    try:
        launch_client(exe, log=log)
    except Exception as exc:
        restore_carrier(backup, root, entries, log=log)
        result['message'] = '启动客户端失败，已回滚：%s' % exc
        return result

    started = time.time()
    live = {}
    while time.time() - started < timeout:
        time.sleep(1.0)
        live = describe_live(root)
        if live.get('uid'):
            break
    live = describe_live(root)
    live_uid = str(live.get('uid') or '')
    result['uid'] = live_uid

    if live_uid and target_uid and live_uid != target_uid:
        _log(log, '落盘 uid=%s 与目标 %s 不一致，自动回滚…' % (live_uid, target_uid), 'warn')
        kill_client(image, log=log)
        restore_carrier(backup, root, entries, log=log)
        try:
            launch_client(exe, log=log)
        except Exception:
            pass
        result.update({'outcome': 'rolled_back',
                       'message': '未进入目标账号，已回滚到换号前的状态'})
        return result

    if not live.get('hasRefresh'):
        kill_client(image, log=log)
        restore_carrier(backup, root, entries, log=log)
        try:
            launch_client(exe, log=log)
        except Exception:
            pass
        result.update({'outcome': 'rolled_back',
                       'message': '客户端启动后没读到登录态，已回滚'})
        return result

    result.update({'ok': True, 'outcome': 'active',
                   'message': '切换完成，当前账号：%s' % (live.get('username') or live_uid)})
    _log(log, '✅ %s（uid %s）' % (result['message'], live_uid), 'ok')
    return result


def rollback(backup_dir, root=None, relaunch=True, log=None):
    """把某个备份目录整组还原回去（手工回滚用）。"""
    root = root or user_data_dir()
    if not os.path.isdir(backup_dir):
        return {'ok': False, 'message': '备份目录不存在：%s' % backup_dir}
    image = client_image_name(root)
    kill_client(image, log=log)
    # 备份目录里的结构就是 root 的相对结构，直接按文件还原
    count = 0
    for base, _dirs, files in os.walk(backup_dir):
        for name in files:
            src = os.path.join(base, name)
            rel = os.path.relpath(src, backup_dir)
            dst = os.path.join(root, rel)
            os.makedirs(os.path.dirname(dst) or '.', exist_ok=True)
            shutil.copy2(src, dst)
            count += 1
    _log(log, '已还原 %d 个文件' % count, 'dim')
    if relaunch:
        exe = _find_exe(os.path.basename(root))
        if exe:
            launch_client(exe, log=log)
    return {'ok': True, 'message': '已还原 %d 个文件' % count}


# 命令行
def _load_panel_accounts():
    try:
        with open(os.path.join(HERE, 'trae_gui_config.json'), 'r', encoding='utf-8') as fh:
            cfg = json.load(fh)
        return [a for a in (cfg.get('accounts') or []) if isinstance(a, dict)]
    except Exception:
        return []


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if '--status' in argv:
        info = describe_live()
        print('客户端目录：%s' % user_data_dir())
        print('登录状态：%s' % ('已登录' if info.get('loggedIn') else '未登录'))
        for key in ('uid', 'username', 'phone', 'deviceId', 'expiredAt'):
            if info.get(key):
                print('  %-10s %s' % (key, info[key]))
        print('载体条目：%s' % (', '.join(detect_entries(user_data_dir())) or '（未探测到）'))
        return 0
    if '--list' in argv:
        for item in _load_panel_accounts():
            print('%-16s uid=%-18s %s' % (item.get('name') or '-', item.get('uid') or '-',
                                          '有refreshToken' if item.get('refreshToken') else '缺凭据'))
        return 0
    if '--switch' in argv:
        idx = argv.index('--switch')
        if idx + 1 >= len(argv):
            print('[X] 缺少账号名')
            return 2
        want = argv[idx + 1]
        accounts = _load_panel_accounts()
        target = next((a for a in accounts
                       if want in (str(a.get('name')), str(a.get('uid')))), None)
        if not target:
            print('[X] 面板配置里没有账号「%s」' % want)
            return 1
        res = switch_to(target, relaunch='--no-launch' not in argv,
                        log=lambda m, lv='dim': print(m))
        print('[%s] %s' % ('OK' if res['ok'] else 'X', res['message']))
        return 0 if res['ok'] else 1
    if '--rollback' in argv:
        idx = argv.index('--rollback')
        if idx + 1 >= len(argv):
            print('[X] 缺少备份目录')
            return 2
        res = rollback(argv[idx + 1], log=lambda m, lv='dim': print(m))
        print(res['message'])
        return 0 if res['ok'] else 1
    print(__doc__)
    print('用法：')
    print('  python trae_switch.py --status            看客户端当前是哪个号')
    print('  python trae_switch.py --list              列出面板里的账号')
    print('  python trae_switch.py --switch 账号名     切换客户端到该账号')
    print('  python trae_switch.py --rollback 备份目录 手工回滚')
    return 0


if __name__ == '__main__':
    sys.exit(main())
