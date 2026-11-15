# -*- coding: utf-8 -*-
"""用隔离配置目录启动客户端，等登录后抓凭据，然后只关掉自己启动的那一份。

日常实例不受影响：每个槽位一个 --user-data-dir，进程按配置目录精确识别。
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE_ROOT = os.path.join(HERE, '.trae_profiles')
INSTANCE_FILE = os.path.join(PROFILE_ROOT, 'instances.json')

LOGIN_KEY = 'iCubeAuthInfo://icube.cloudide'
DC_PREFIX = 'iCubeAuthInfo://icube-dc'
STORAGE_REL = ('User', 'globalStorage', 'storage.json')
DEFAULT_TIMEOUT = 300
IS_WIN = os.name == 'nt'
CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

# 这些变量必须从子进程环境里剔除，否则客户端会「退化」：
#   ELECTRON_RUN_AS_NODE 会让 Electron 当纯 Node 跑，直接报 bad option: --user-data-dir
#   NODE_OPTIONS 会注入外部 --require，导致启动即崩
#   VSCODE_IPC_HOOK_CLI 会让新实例去连已有的窗口，隔离就失效了
DROP_ENV = ('ELECTRON_RUN_AS_NODE', 'NODE_OPTIONS', 'VSCODE_IPC_HOOK_CLI',
            'ELECTRON_NO_ATTACH_CONSOLE', 'ELECTRON_ENABLE_LOGGING')

# 产品线：名字 → (内部 id, 界面名, 可执行文件名候选)
PRODUCTS = (
    ('trae', 'TRAE CN', ('Trae CN.exe', 'Trae.exe')),
    ('solo', 'TRAE SOLO CN', ('TRAE SOLO CN.exe', 'TRAE SOLO.exe')),
)

DEVICE_ID_RE = re.compile(r'^(?:\d{12,20}|aha-[0-9a-fA-F]{8,64})$')


# 日志与子进程小工具
def _default_log(text, level='dim'):
    print(text, flush=True)


def _log(log, text, level='dim'):
    (log or _default_log)(text, level)


def child_env(base=None):
    """启动客户端用的干净环境（去掉会让 Electron 变质的那几个变量）。"""
    env = dict(base if base is not None else os.environ)
    for key in DROP_ENV:
        env.pop(key, None)
    return env


def _ps(script, timeout=25):
    """跑一段 PowerShell 并取回 stdout；失败返回空串（调用方按「查不到」处理）。"""
    if not IS_WIN:
        return ''
    try:
        proc = subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive',
             '-ExecutionPolicy', 'Bypass', '-Command', script],
            capture_output=True, timeout=timeout, creationflags=CREATE_NO_WINDOW)
        return (proc.stdout or b'').decode('utf-8', 'replace')
    except Exception:
        return ''


# 客户端定位
def _registry_dirs():
    """从「应用和功能」卸载表里找 Trae 的安装目录。"""
    if not IS_WIN:
        return []
    out = []
    try:
        import winreg
    except Exception:
        return []
    roots = [
        (winreg.HKEY_CURRENT_USER, r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'),
        (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'),
        (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'),
    ]
    for hive, path in roots:
        try:
            root = winreg.OpenKey(hive, path)
        except OSError:
            continue
        index = 0
        while True:
            try:
                name = winreg.EnumKey(root, index)
            except OSError:
                break
            index += 1
            try:
                sub = winreg.OpenKey(root, name)
            except OSError:
                continue
            try:
                display = str(winreg.QueryValueEx(sub, 'DisplayName')[0] or '')
            except OSError:
                display = ''
            if 'trae' not in display.lower():
                continue
            try:
                where = str(winreg.QueryValueEx(sub, 'InstallLocation')[0] or '')
            except OSError:
                where = ''
            where = where.strip().strip('"').rstrip('\\')
            if where and where not in out:
                out.append(where)
    return out


def _common_dirs():
    home = os.path.expanduser('~')
    local = os.environ.get('LOCALAPPDATA') or os.path.join(home, 'AppData', 'Local')
    dirs = []
    for name in ('Trae CN', 'Trae', 'TRAE SOLO CN'):
        dirs.append(os.path.join(local, 'Programs', name))
        dirs.append(os.path.join(os.environ.get('PROGRAMFILES', r'C:\Program Files'), name))
        dirs.append(os.path.join(os.environ.get('PROGRAMFILES(X86)', r'C:\Program Files (x86)'), name))
    return dirs


def find_clients():
    """返回本机可用客户端列表：[{'id','label','exe','dir'}]，按产品线去重。"""
    found, seen = [], set()

    def take(exe, label):
        exe = os.path.abspath(str(exe).strip().strip('"'))
        if not os.path.isfile(exe):
            return
        key = exe.lower()
        if key in seen:
            return
        seen.add(key)
        found.append({'id': label.split()[0].lower(), 'label': label,
                      'exe': exe, 'dir': os.path.dirname(exe)})

    env_exe = os.getenv('TRAE_EXE', '').strip()
    if env_exe:
        take(env_exe, 'TRAE CN')

    for base in _registry_dirs() + _common_dirs():
        if not os.path.isdir(base):
            continue
        for product, label, names in PRODUCTS:
            for name in names:
                take(os.path.join(base, name), label)
    return found


def pick_client(clients=None, preferred='', product=''):
    """挑一个客户端：显式指定 > 产品线匹配 > 第一个。"""
    clients = clients if clients is not None else find_clients()
    preferred = str(preferred or '').strip()
    if preferred:
        for item in clients:
            if item['exe'].lower() == os.path.abspath(preferred).lower():
                return item
        if os.path.isfile(preferred):
            return {'id': 'custom', 'label': os.path.basename(preferred),
                    'exe': os.path.abspath(preferred), 'dir': os.path.dirname(preferred)}
    if product:
        for item in clients:
            if item['id'] == product:
                return item
    return clients[0] if clients else None


# 槽位 / 配置目录
def slugify(text, width=22):
    """把槽位名变成安全的目录名：只留 ASCII，中文名靠短哈希保证唯一。"""
    raw = str(text or '').strip()
    safe = re.sub(r'[^0-9A-Za-z._-]+', '-', raw).strip('-._')[:width]
    digest = hashlib.sha1(raw.encode('utf-8')).hexdigest()[:8]
    return ('%s-%s' % (safe, digest)) if safe else ('slot-%s' % digest)


def profile_name(slot):
    """槽位对应的配置目录名（槽位里存了就用存的，保证改名不影响已登录状态）。"""
    name = str((slot or {}).get('profile') or '').strip()
    if name:
        return name
    return slugify((slot or {}).get('name') or (slot or {}).get('key') or 'slot')


def profile_dir(slot_or_name):
    name = slot_or_name if isinstance(slot_or_name, str) else profile_name(slot_or_name)
    return os.path.join(PROFILE_ROOT, name)


def storage_file(profile):
    path = profile if os.path.basename(str(profile)) == 'storage.json' else os.path.join(profile, *STORAGE_REL)
    return path


def _profile_name_of_path(path):
    """从配置目录路径反推目录名（用于展示与清理）。"""
    try:
        rel = os.path.relpath(os.path.abspath(path), PROFILE_ROOT)
    except Exception:
        return ''
    return '' if rel.startswith('..') else rel.replace('\\', '/')


def list_profiles():
    """已存在的隔离配置目录名列表。"""
    if not os.path.isdir(PROFILE_ROOT):
        return []
    return sorted(n for n in os.listdir(PROFILE_ROOT)
                  if os.path.isdir(os.path.join(PROFILE_ROOT, n)))


# 凭据解密与读取
def _decrypt_value(text):
    """解密 storage.json 里的 tc 值；已经是明文 JSON 时原样返回。

    先用 trae_get_token 的纯标准库 AES（零依赖），失败再退回 trae_checkin
    （它需要 cryptography，只在新格式 tc05 上才有差异时才用得上）。
    """
    text = (text or '').strip()
    if not text:
        return ''
    if text.startswith('{'):
        return text
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    try:
        import trae_get_token as token_tool
        return token_tool.decrypt_storage_value(text).decode('utf-8', 'replace')
    except Exception as first:
        try:
            import trae_checkin as checkin
            return checkin.decrypt_storage_value(text)
        except Exception:
            raise ValueError('凭据解密失败：%s' % first)


def dc_device_ids(storage):
    """storage 里登记过的设备号（新版客户端会同时留着旧数字号与 aha- 号）。"""
    out = []
    for key in storage:
        if not str(key).startswith(DC_PREFIX):
            continue
        rest = str(key)[len(DC_PREFIX):].lstrip(':').strip()
        if DEVICE_ID_RE.match(rest) and rest not in out:
            out.append(rest)
    return out


def pick_device_id(ids):
    """多份设备身份时挑「当前这份」：优先 aha-（新版迁移后的号），否则取最后一个。"""
    ids = [i for i in (ids or []) if DEVICE_ID_RE.match(str(i))]
    if not ids:
        return ''
    for item in ids:
        if item.startswith('aha-'):
            return item
    return ids[-1]


def device_identity(storage, device_id=''):
    """取指定设备号对应的密钥对；没指定就按 pick_device_id 挑一份。"""
    ids = dc_device_ids(storage)
    target = str(device_id or '').strip() or pick_device_id(ids)
    candidates = [target] if target else ids
    for did in candidates:
        raw = storage.get('%s:%s' % (DC_PREFIX, did))
        if not raw:
            continue
        try:
            dev = json.loads(_decrypt_value(raw))
        except Exception:
            continue
        priv = str(dev.get('privateKeyPEM') or '').strip()
        pub = str(dev.get('publicKeyPEM') or '').strip()
        if priv and pub:
            return {'deviceId': did, 'deviceKeyPem': priv, 'devicePubPem': pub}
    return {'deviceId': target, 'deviceKeyPem': '', 'devicePubPem': ''}


def read_credentials(profile):
    """读某个配置目录当前的登录态；没登录 / 文件正在写 / 解密失败都返回 None。"""
    path = storage_file(profile)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            storage = json.load(fh)
    except Exception:
        return None
    if not isinstance(storage, dict):
        return None
    enc = storage.get(LOGIN_KEY)
    if not enc:
        return None
    try:
        creds = json.loads(_decrypt_value(enc))
    except Exception:
        return None
    if not isinstance(creds, dict):
        return None
    refresh = str(creds.get('refreshToken') or '').strip()
    if not refresh:
        return None
    acc = creds.get('account') or {}
    dev = device_identity(storage)
    return {
        'refreshToken': refresh,
        'accessToken': str(creds.get('token') or creds.get('accessToken') or '').strip(),
        'uid': str(creds.get('userId') or '').strip(),
        'username': str(acc.get('username') or '').strip(),
        'phone': str(acc.get('nonPlainTextMobile') or '').strip(),
        'deviceId': dev['deviceId'],
        'deviceKeyPem': dev['deviceKeyPem'],
        'devicePubPem': dev['devicePubPem'],
        'machineId': str(storage.get('telemetry.machineId') or '').strip(),
        'expiredAt': str(creds.get('expiredAt') or ''),
        'profile': _profile_name_of_path(profile),
        'storage': storage_file(profile),
        'mtime': os.path.getmtime(path),
    }


def account_from(creds, name=''):
    """把抓到的凭据整理成面板账号条目（字段名与 trae_gui / trae_checkin 对齐）。"""
    did = str(creds.get('deviceId') or '').strip()
    acc = {
        'name': str(name or creds.get('username') or creds.get('uid') or '').strip(),
        'uid': str(creds.get('uid') or '').strip(),
        'refreshToken': str(creds.get('refreshToken') or '').strip(),
        'accessToken': str(creds.get('accessToken') or '').strip(),
        'deviceKeyPem': str(creds.get('deviceKeyPem') or '').strip(),
        'devicePubPem': str(creds.get('devicePubPem') or '').strip(),
        'machineId': str(creds.get('machineId') or '').strip(),
        'profile': str(creds.get('profile') or ''),
        'source': '隔离客户端登录',
        'enabled': True,
    }
    if did:
        acc['realDeviceId'] = did
        acc['deviceId'] = did
    return acc


# 进程枚举 / 关闭
def _pid_alive(pid):
    try:
        pid = int(pid)
    except Exception:
        return False
    if pid <= 0:
        return False
    if not IS_WIN:
        try:
            os.kill(pid, 0)
            return True
        except Exception:
            return False
    import ctypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
    if not handle:
        return ctypes.windll.kernel32.GetLastError() == 5   # 拒绝访问 = 进程还在
    try:
        code = ctypes.c_ulong()
        if ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return code.value == 259                        # STILL_ACTIVE
        return True
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _scan_pairs(marker):
    """枚举命令行里含 marker 的进程，返回 [(pid, ppid)]。"""
    if not IS_WIN or not marker:
        return []
    escaped = str(marker).replace("'", "''")
    script = (
        "$m='%s'; "
        "Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | "
        "Where-Object { $_.CommandLine -and $_.CommandLine.IndexOf($m, "
        "[System.StringComparison]::OrdinalIgnoreCase) -ge 0 } | "
        "ForEach-Object { \"$($_.ProcessId)|$($_.ParentProcessId)\" }" % escaped)
    pairs = []
    for line in _ps(script).splitlines():
        parts = line.strip().split('|')
        if len(parts) == 2 and parts[0].isdigit() and parts[1].split('.')[0].isdigit():
            pairs.append((int(parts[0]), int(parts[1].split('.')[0])))
    return pairs


def instance_pids(profile, include_children=True):
    """属于该配置目录的所有进程号（含 renderer / gpu 等子进程）。"""
    path = os.path.abspath(profile if isinstance(profile, str) else profile_dir(profile))
    pairs = _scan_pairs(path)
    pids = {pid for pid, _ in pairs}
    if include_children and pairs:
        children = {}
        for pid, ppid in pairs:
            children.setdefault(ppid, []).append(pid)
        stack = list(pids)
        while stack:
            for child in children.get(stack.pop(), []):
                if child not in pids:
                    pids.add(child)
                    stack.append(child)
    recorded = _load_instances().get(_profile_name_of_path(path))
    if recorded and _pid_alive(recorded):
        pids.add(int(recorded))
    return sorted(pids)


def _post_wm_close(pids):
    """给可见窗口发 WM_CLOSE，让客户端自己走正常退出（少丢数据）。"""
    if not IS_WIN or not pids:
        return 0
    import ctypes
    user32 = ctypes.windll.user32
    targets = set(int(p) for p in pids)
    sent = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def callback(hwnd, lparam):
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in targets and user32.IsWindowVisible(hwnd):
            user32.PostMessageW(hwnd, 0x0010, 0, 0)
            sent.append(pid.value)
        return True

    try:
        user32.EnumWindows(callback, 0)
    except Exception:
        return 0
    return len(sent)


def _stop_pids(pids):
    """强杀指定进程。优先 Stop-Process，退而求其次 taskkill。"""
    pids = sorted({int(p) for p in pids if p})
    if not pids:
        return 0
    if IS_WIN:
        _ps('Stop-Process -Id %s -Force -ErrorAction SilentlyContinue'
            % ','.join(str(p) for p in pids))
        time.sleep(0.8)
    left = [p for p in pids if _pid_alive(p)]
    if left and IS_WIN:
        try:
            subprocess.run(['taskkill', '/F'] + sum([['/PID', str(p)] for p in left], []),
                           capture_output=True, timeout=15, creationflags=CREATE_NO_WINDOW)
        except Exception:
            pass
        time.sleep(0.6)
        left = [p for p in left if _pid_alive(p)]
    return len(left)


def close_instance(profile, log=None, graceful=8.0, extra_pids=()):
    """只关掉这个配置目录对应的实例，返回残留进程数（0 = 关干净了）。"""
    path = os.path.abspath(profile if isinstance(profile, str) else profile_dir(profile))
    name = _profile_name_of_path(path) or os.path.basename(path)
    pids = set(instance_pids(path))
    pids |= {int(p) for p in extra_pids if p}
    pids = {p for p in pids if _pid_alive(p)}
    if not pids:
        _unregister(name)
        return 0
    _log(log, '正在关闭隔离实例（%s）：%d 个进程…' % (name, len(pids)), 'dim')
    _post_wm_close(pids)
    deadline = time.time() + max(0.0, graceful)
    while time.time() < deadline:
        if not any(_pid_alive(p) for p in pids):
            break
        time.sleep(0.5)
    left = {p for p in pids if _pid_alive(p)}
    if left:
        # WM_CLOSE 没走掉（比如停在「有未保存内容」的对话框上），强杀兜底
        _stop_pids(left)
        left = {p for p in pids if _pid_alive(p)}
    # 再扫一遍：主进程被强杀后可能还有孤儿 helper
    for pid in instance_pids(path):
        if _pid_alive(pid):
            left.add(pid)
    if left:
        _stop_pids(left)
        left = {p for p in left if _pid_alive(p)}
    if not left:
        _unregister(name)
    return len(left)


def close_all_instances(log=None):
    """关闭所有隔离实例（只认 .trae_profiles 下的目录，绝不碰日常实例）。"""
    closed, left = [], 0
    names = set(list_profiles())
    entries = _load_instances()
    names |= set(entries.keys())
    if os.path.isdir(PROFILE_ROOT):
        marker = os.path.abspath(PROFILE_ROOT).lower()
        for line in _ps("Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | "
                        "ForEach-Object { $_.CommandLine }").splitlines():
            text = line.lower()
            if marker in text and '--user-data-dir' in text:
                # 命令行里带 .trae_profiles 的进程，取它下面的目录名
                tail = text.split(marker, 1)[1].lstrip('\\/').split('"')[0]
                tail = tail.split(' ')[0].strip('\\/')
                if tail and tail not in names:
                    names.add(tail.split('\\')[0])
    for name in sorted(names):
        try:
            remain = close_instance(os.path.join(PROFILE_ROOT, name), log=log, graceful=6.0)
        except Exception as exc:
            remain = -1
            _log(log, '关闭 %s 失败：%s' % (name, exc), 'err')
        if remain == 0:
            closed.append(name)
        else:
            left += 1
    return {'closed': closed, 'failed': left}


# 实例登记（进程枚举失败时的兜底）
def _load_instances():
    try:
        with open(INSTANCE_FILE, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_instances(data):
    try:
        os.makedirs(PROFILE_ROOT, exist_ok=True)
        tmp = INSTANCE_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, INSTANCE_FILE)
    except Exception:
        pass


def _register(profile, pid):
    data = _load_instances()
    data[_profile_name_of_path(profile) or os.path.basename(profile)] = int(pid)
    _save_instances(data)


def _unregister(profile):
    data = _load_instances()
    key = _profile_name_of_path(profile) or os.path.basename(profile)
    if key in data:
        data.pop(key, None)
        _save_instances(data)


# 启动 / 等待登录
def launch_instance(profile, exe, extra_args=(), log=None):
    """用独立配置目录拉起客户端，返回 Popen（pid 会登记，便于异常时兜底关闭）。"""
    if not os.path.isfile(exe):
        raise FileNotFoundError('找不到客户端：%s' % exe)
    path = os.path.abspath(profile if isinstance(profile, str) else profile_dir(profile))
    os.makedirs(path, exist_ok=True)
    args = [exe, '--user-data-dir=%s' % path] + list(extra_args or ())
    proc = subprocess.Popen(args, env=child_env(), close_fds=True,
                            cwd=os.path.dirname(exe) or None)
    _register(path, proc.pid)
    _log(log, '已启动：%s（pid=%s）' % (os.path.basename(exe), proc.pid), 'dim')
    return proc


def wait_login(profile, timeout=DEFAULT_TIMEOUT, stop=None, log=None,
               baseline=None, launched_pid=None, tick=2.0, idle_grace=0):
    """等这个配置目录出现登录凭据。

    baseline 传入启动前的凭据快照时，只有凭据**发生变化**才算新登录成功；
    没传 baseline 而目录里本来就有凭据，则直接判定为「复用已有登录」。
    """
    path = os.path.abspath(profile if isinstance(profile, str) else profile_dir(profile))
    started = time.time()
    last_note = 0
    while True:
        if stop is not None and getattr(stop, 'is_set', lambda: False)():
            return {'ok': False, 'error': '已取消', 'waited': time.time() - started,
                    'account': None, 'reused': False}
        creds = read_credentials(path)
        if creds:
            if baseline is None:
                return {'ok': True, 'account': creds, 'waited': time.time() - started,
                        'reused': True}
            changed = (creds.get('refreshToken') != baseline.get('refreshToken')
                       or creds.get('uid') != baseline.get('uid')
                       or creds.get('mtime', 0) > float(baseline.get('mtime') or 0) + 0.5)
            if changed:
                return {'ok': True, 'account': creds, 'waited': time.time() - started,
                        'reused': False}
            # 目录里还是旧凭据：等客户端刷新一次，最多等到 idle_grace
            if idle_grace and time.time() - started > idle_grace:
                return {'ok': True, 'account': creds, 'waited': time.time() - started,
                        'reused': True}
        elapsed = time.time() - started
        if launched_pid and elapsed > 20 and not _pid_alive(launched_pid) \
                and not instance_pids(path):
            return {'ok': False, 'error': '客户端已退出（登录没完成）',
                    'waited': elapsed, 'account': None, 'reused': False}
        if elapsed > timeout:
            return {'ok': False, 'error': '等待登录超时（%d 秒）' % int(timeout),
                    'waited': elapsed, 'account': None, 'reused': False}
        if log and time.time() - last_note > 30:
            last_note = time.time()
            _log(log, '仍在等待登录…（已等 %d 秒，请在那个 Trae 窗口完成登录）'
                 % int(elapsed), 'dim')
        time.sleep(max(0.5, tick))


def login_slot(slot, client, timeout=DEFAULT_TIMEOUT, reuse=True, fresh=False,
               keep_open=False, stop=None, log=None, prewait=6.0):
    """完整跑一个槽位：启动 → 等登录 → 抓凭据 → 关闭。

    返回 {'ok','profile','account','reused','waited','error','closed','slot'}
    """
    slot = dict(slot or {})
    name = str(slot.get('name') or slot.get('key') or '槽位').strip()
    prof_name = profile_name(slot)
    path = profile_dir(prof_name)
    result = {'ok': False, 'profile': prof_name, 'account': None, 'reused': False,
              'waited': 0.0, 'error': '', 'closed': True, 'slot': slot}

    if not client or not client.get('exe'):
        result['error'] = '没找到 Trae 客户端（可在设置里手动指定 exe）'
        return result

    if fresh and os.path.isdir(path):
        _log(log, '清空配置目录 %s（会要求重新登录）…' % prof_name, 'warn')
        try:
            close_instance(path, log=log, graceful=5.0)
            shutil.rmtree(path, ignore_errors=True)
        except Exception as exc:
            _log(log, '清空配置目录失败：%s' % exc, 'err')

    baseline = read_credentials(path)
    if baseline and not reuse:
        _log(log, '配置目录里已有登录态，但未开启复用，改为重新登录…', 'dim')
    if baseline and not reuse and os.path.isdir(path):
        try:
            close_instance(path, log=log, graceful=5.0)
            shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass
        baseline = None

    if baseline and reuse:
        _log(log, '「%s」上次已登录（%s），直接复用配置目录，开始抓取当前凭据…'
             % (name, baseline.get('username') or baseline.get('uid')), 'ok')

    # 同一目录只允许一个实例：启动前先确保没有残留
    leftover = instance_pids(path)
    if leftover:
        _log(log, '检测到该配置目录还有 %d 个残留进程，先关掉再启动…' % len(leftover), 'warn')
        close_instance(path, log=log, graceful=6.0)

    pid = None
    try:
        proc = launch_instance(path, client['exe'], log=log)
        pid = proc.pid
    except Exception as exc:
        result['error'] = '启动客户端失败：%s' % exc
        return result

    if baseline and reuse:
        # 复用场景：给客户端几秒把 token 刷新到盘上，然后按旧凭据兜底返回
        outcome = wait_login(path, timeout=min(timeout, max(12, prewait + 10)), stop=stop,
                             log=None, baseline=baseline, launched_pid=pid,
                             idle_grace=prewait)
    else:
        hint = slot.get('phone') or ''
        _log(log, '请在弹出的 Trae 窗口里登录%s（手机号 + 验证码；不超过 %d 秒）…'
             % (('「%s」' % hint) if hint else '「%s」' % name, timeout), 'head')
        outcome = wait_login(path, timeout=timeout, stop=stop, log=log, baseline=None,
                             launched_pid=pid)

    result['waited'] = outcome.get('waited') or 0
    if not outcome.get('ok'):
        result['error'] = outcome.get('error') or '登录失败'
        if not keep_open:
            remain = close_instance(path, log=log, extra_pids=[pid])
            result['closed'] = remain == 0
        return result

    creds = outcome.get('account') or {}
    account = account_from(creds, name=name)
    result['account'] = account
    result['reused'] = bool(outcome.get('reused'))
    result['ok'] = bool(account.get('refreshToken'))
    if not result['ok']:
        result['error'] = '读到了登录态但拿不到 refreshToken'
    if not keep_open:
        remain = close_instance(path, log=log, extra_pids=[pid])
        result['closed'] = remain == 0
        if remain:
            _log(log, '警告：还有 %d 个进程没关掉，可在本页点「关闭所有隔离实例」' % remain, 'warn')
    return result


def run_batch(slots, client, timeout=DEFAULT_TIMEOUT, reuse=True, fresh_all=False,
              keep_open=False, stop=None, log=None, auto_checkin=None,
              on_result=None):
    """按顺序跑多个槽位（换号）。每个槽位结束后回调 on_result(slot, result)。"""
    slots = [dict(s) for s in (slots or []) if s]
    results = []
    total = len(slots)
    for index, slot in enumerate(slots, 1):
        if stop is not None and getattr(stop, 'is_set', lambda: False)():
            _log(log, '已中止，剩余 %d 个槽位未处理' % (total - index + 1), 'warn')
            break
        name = str(slot.get('name') or slot.get('key') or '槽位')
        _log(log, '── 第 %d/%d 个：%s ──' % (index, total, name), 'head')
        res = login_slot(slot, client, timeout=timeout, reuse=reuse,
                         fresh=bool(slot.get('fresh')) or fresh_all, keep_open=keep_open,
                         stop=stop, log=log)
        results.append(res)
        if res.get('ok'):
            acc = res['account']
            _log(log, '✅「%s」已抓到凭据：%s（UID %s，设备号 %s）'
                 % (name, acc.get('name') or '-', acc.get('uid') or '-',
                    acc.get('realDeviceId') or '无'), 'ok')
        else:
            _log(log, '❌「%s」失败：%s' % (name, res.get('error') or '未知原因'), 'err')
        if on_result:
            try:
                on_result(slot, res)
            except Exception as exc:
                _log(log, '回调处理异常：%s' % exc, 'err')
    ok = sum(1 for r in results if r.get('ok'))
    return {'ok': ok, 'fail': len(results) - ok, 'total': total, 'results': results}


# 自检 / 命令行
def doctor():
    """环境自检：客户端、配置根目录、PowerShell 枚举、残留实例。"""
    clients = find_clients()
    info = {
        'platform': sys.platform,
        'profile_root': PROFILE_ROOT,
        'profile_root_exists': os.path.isdir(PROFILE_ROOT),
        'clients': clients,
        'powershell': bool(IS_WIN and _ps('$PSVersionTable.PSVersion.Major')),
        'profiles': list_profiles(),
        'instances': _load_instances(),
        'env_clean': all(k not in child_env() for k in DROP_ENV),
    }
    info['running'] = {name: instance_pids(os.path.join(PROFILE_ROOT, name))
                       for name in info['profiles']}
    return info


def _print_doctor():
    info = doctor()
    print('=== Trae 自动登录环境自检 ===')
    print('配置根目录 : %s（%s）' % (info['profile_root'],
                                   '已创建' if info['profile_root_exists'] else '尚未创建'))
    print('子进程环境 : %s' % ('已剔除 ELECTRON_RUN_AS_NODE / NODE_OPTIONS'
                              if info['env_clean'] else '仍有污染变量！'))
    print('PowerShell : %s' % ('可用' if info['powershell'] else '不可用（进程枚举会退化）'))
    if not info['clients']:
        print('客户端     : ✗ 没找到，请设置 TRAE_EXE 环境变量指向 Trae CN.exe')
    for item in info['clients']:
        print('客户端     : ✓ %s  %s' % (item['label'], item['exe']))
    print('槽位目录   : %s' % (', '.join(info['profiles']) or '（空）'))
    for name, pids in (info['running'] or {}).items():
        if pids:
            print('残留实例   : %s -> %s' % (name, pids))
    return 0 if info['clients'] else 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or '--help' in argv or '-h' in argv:
        print(__doc__)
        print('用法：')
        print('  python trae_autologin.py --doctor              环境自检')
        print('  python trae_autologin.py --list                列出可用客户端')
        print('  python trae_autologin.py --login 名字 [--fresh] [--keep-open]')
        print('  python trae_autologin.py --close-all           关闭所有隔离实例')
        return 0
    if '--doctor' in argv:
        return _print_doctor()
    if '--list' in argv:
        for item in find_clients():
            print('%-14s %s' % (item['label'], item['exe']))
        return 0
    if '--close-all' in argv:
        res = close_all_instances(log=lambda m, lv='dim': print(m))
        print('已关闭：%s；失败：%d' % (', '.join(res['closed']) or '无', res['failed']))
        return 0
    if '--login' in argv:
        idx = argv.index('--login')
        name = argv[idx + 1] if idx + 1 < len(argv) and not argv[idx + 1].startswith('-') else '账号'
        timeout = DEFAULT_TIMEOUT
        if '--timeout' in argv:
            try:
                timeout = int(argv[argv.index('--timeout') + 1])
            except Exception:
                pass
        client = pick_client()
        if not client:
            print('[X] 没找到 Trae 客户端，可设置 TRAE_EXE 环境变量指向 exe。')
            return 1
        res = login_slot({'name': name}, client, timeout=timeout,
                         fresh='--fresh' in argv, keep_open='--keep-open' in argv,
                         log=lambda m, lv='dim': print(m))
        if res['ok']:
            acc = res['account']
            print('=' * 60)
            print('登录成功：%s  UID %s  设备号 %s' % (acc.get('name'), acc.get('uid'),
                                                     acc.get('realDeviceId')))
            print('refreshToken: %s' % acc.get('refreshToken'))
            print('=' * 60)
            return 0
        print('[X] 失败：%s' % res.get('error'))
        return 1
    print('[X] 未知参数：%s（用 --help 查看用法）' % ' '.join(argv))
    return 2


if __name__ == '__main__':
    sys.exit(main())
