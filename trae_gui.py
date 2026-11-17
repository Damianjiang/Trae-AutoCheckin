# -*- coding: utf-8 -*-
"""Trae 自动签到 · 桌面面板（零依赖 tkinter）

给不想碰环境变量和命令行的用户使用：
  • 「账号列表」：一键读取本机 Trae / TRAE SOLO 登录态，也可手动增删改多个账号
  • 「设置」：每天定时签到（默认 08:00）、失败自动补签、推送、设备密钥
  • 「运行日志」：实时显示签到过程，并落盘到 logs/ 便于排查
  • 支持浅色 / 深色主题切换

签到逻辑仍由 trae_checkin.py 完成，本面板只负责配置、调度与展示：
  配置 -> TRAE_CONFIG(JSON) 环境变量 -> 子进程运行 trae_checkin.py

双击同目录的「启动签到面板.vbs」即可打开本面板。
"""
import base64
import ctypes
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

# ══════════════════════════════════════════════════════════════
# 常量与路径
# ══════════════════════════════════════════════════════════════
HERE = os.path.dirname(os.path.abspath(__file__))
CHECKIN_SCRIPT = os.path.join(HERE, 'trae_checkin.py')
TOKEN_SCRIPT = os.path.join(HERE, 'trae_get_token.py')
WEBLOGIN_SCRIPT = os.path.join(HERE, 'trae_weblogin.py')
SWITCH_SCRIPT = os.path.join(HERE, 'trae_switch.py')
CONFIG_FILE = os.path.join(HERE, 'trae_gui_config.json')
STATE_FILE = os.path.join(HERE, '.trae_checkin_state.json')
LOG_DIR = os.path.join(HERE, 'logs')
VBS_LAUNCHER = os.path.join(HERE, '启动签到面板.vbs')

# 网页登录 / 换号引擎：缺文件时面板照常可用，只是对应按钮会提示
try:
    import trae_weblogin as WEB
except Exception:                                    # noqa: BLE001
    WEB = None
try:
    import trae_switch as SWITCH
except Exception:                                    # noqa: BLE001
    SWITCH = None

APP_TITLE = 'Trae 自动签到'
APP_VERSION = 'v1.2.3'
UI_FONT = 'Microsoft YaHei UI'

BJ_TZ = timezone(timedelta(hours=8))   # 与 trae_checkin.py 的北京时间保持一致
CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

# 浅色 / 深色两套配色：所有控件通过角色取色，切换时统一重绘
THEMES = {
    'light': {
        'bg': '#f2f5fa', 'card': '#ffffff', 'border': '#e2e8f2',
        'text': '#1b2430', 'muted': '#6b7787',
        'accent': '#2f6fed', 'accent_hover': '#215bd6', 'accent_fg': '#ffffff',
        'btn_bg': '#ffffff', 'btn_fg': '#1b2430', 'btn_hover': '#eef3fb',
        'danger_hover': '#fdecec',
        'input_bg': '#f9fbfe', 'input_fg': '#1b2430', 'input_border': '#d7e0ee',
        'tree_head': '#f7f9fd', 'tree_sel': '#e7f0ff', 'row_alt': '#fafcff',
        'header_bg': '#101a2b', 'header_fg': '#ffffff', 'header_sub': '#8fa1bd',
        'chip_bg': '#1c2940', 'chip_fg': '#d9e6ff', 'chip_hover': '#27395a',
        'log_bg': '#0b1220', 'log_fg': '#d6e2f5',
        'ok': '#15803d', 'warn': '#b45309', 'err': '#dc2626', 'off': '#9aa5b5',
    },
    'dark': {
        'bg': '#0c1320', 'card': '#141e2e', 'border': '#223049',
        'text': '#e7eefb', 'muted': '#93a4bd',
        'accent': '#5b93ff', 'accent_hover': '#4a82f0', 'accent_fg': '#ffffff',
        'btn_bg': '#1b2739', 'btn_fg': '#e7eefb', 'btn_hover': '#24344c',
        'danger_hover': '#3a1f27',
        'input_bg': '#0f1927', 'input_fg': '#e7eefb', 'input_border': '#2a3a55',
        'tree_head': '#182334', 'tree_sel': '#253a5c', 'row_alt': '#16202f',
        'header_bg': '#080f19', 'header_fg': '#f2f7ff', 'header_sub': '#8fa1bd',
        'chip_bg': '#1b2a42', 'chip_fg': '#d5e5ff', 'chip_hover': '#25395a',
        'log_bg': '#070d17', 'log_fg': '#cfe0f5',
        'ok': '#4ade80', 'warn': '#fbbf24', 'err': '#f87171', 'off': '#64748b',
    },
}

CONFIG_DEFAULTS = {
    'version': 1,
    'accounts': [],
    'device': {'keyPem': '', 'pubPem': '', 'machineId': '', 'deviceId': '',
               'name': '', 'model': '', 'brand': ''},
    'push': {'qywxToken': '', 'plusplusToken': ''},
    'schedule': {'time': '08:00', 'retryEnabled': True, 'retryIntervalMin': 60,
                 'retryMax': 6, 'retryDeadline': '23:30', 'catchUp': True},
    'ui': {'theme': 'light'},
    'lastAutoDate': '',
}

# 脚本最终会按 "图标 [状态] 名称：明细" 打印每个账号的结果
RESULT_RE = re.compile(
    r'^.*?\[(签到成功|已签到|待重试|失败|异常|未开放|鉴权失败)\]\s*(.+?)：\s*(.*)$')
BAD_STATUS = ('待重试', '失败', '异常', '未开放', '鉴权失败')

# 客户端真实设备号：旧版是 12~20 位数字，新版（TRAE SOLO CN 等）是 aha-<十六进制>
DEVICE_ID_RE = re.compile(r'^(?:\d{12,20}|aha-[0-9a-fA-F]{8,64})$')

STARTUP_DIR = os.path.join(
    os.environ.get('APPDATA', ''),
    'Microsoft', 'Windows', 'Start Menu', 'Programs', 'Startup')
STARTUP_LNK = os.path.join(STARTUP_DIR, 'Trae自动签到面板.lnk')


# ══════════════════════════════════════════════════════════════
# 通用小工具
# ══════════════════════════════════════════════════════════════
def bj_now():
    return datetime.now(BJ_TZ)


def today_str():
    return bj_now().strftime('%Y-%m-%d')


def parse_hhmm(text, default=None):
    """'8:00' / '08:00' -> (8, 0)；解析失败返回 default。"""
    text = str(text or '').strip().replace('：', ':')
    m = re.fullmatch(r'(\d{1,2}):(\d{2})', text)
    if not m:
        return default
    h, mi = int(m.group(1)), int(m.group(2))
    if 0 <= h <= 23 and 0 <= mi <= 59:
        return h, mi
    return default


def read_json(path, default=None):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def write_json_atomic(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def mask_token(value):
    value = str(value or '')
    if len(value) <= 16:
        return value[:3] + '…' if value else ''
    return value[:10] + '…' + value[-6:]


def jwt_uid(token):
    """从 accessToken 里解出 uid（解不出来就返回空串）。"""
    try:
        part = str(token or '').split('.')[1]
        part += '=' * (-len(part) % 4)
        data = json.loads(base64.urlsafe_b64decode(part))
        return str((data.get('data') or {}).get('id') or '')
    except Exception:
        return ''


def jwt_exp(token):
    """accessToken 的过期时间戳；解不出来返回 0。"""
    try:
        part = str(token or '').split('.')[1]
        part += '=' * (-len(part) % 4)
        data = json.loads(base64.urlsafe_b64decode(part))
        return int(data.get('exp') or 0)
    except Exception:
        return 0


def acct_key(acc):
    """与 trae_checkin.py 的 _acct_key 保持一致：uid > 名称。"""
    return (str(acc.get('uid') or '').strip()
            or str(acc.get('name') or '').strip()
            or 'default')


def read_state():
    st = read_json(STATE_FILE, {}) or {}
    if st.get('date') != today_str():
        st = {'date': today_str(), 'done': {}, 'cooldown': {}}
    st.setdefault('done', {})
    st.setdefault('cooldown', {})
    return st


def cooldown_left(until_text):
    try:
        ts = time.mktime(time.strptime(str(until_text), '%Y-%m-%d %H:%M'))
    except Exception:
        return 0
    return max(0, int(ts - time.time()))


def text_get(widget):
    return widget.get('1.0', 'end-1c').strip()


def text_set(widget, value):
    widget.delete('1.0', 'end')
    widget.insert('1.0', value or '')


# ══════════════════════════════════════════════════════════════
# 开机自动启动
# ══════════════════════════════════════════════════════════════
def startup_enabled():
    return os.path.isfile(STARTUP_LNK)


def _ps_quote(value):
    """PowerShell 单引号字符串转义。"""
    return "'" + str(value).replace("'", "''") + "'"


def set_startup_enabled(enabled):
    """写入/删除「启动」文件夹里的快捷方式，返回 (是否成功, 提示)。"""
    if os.name != 'nt':
        return False, '开机自动启动仅支持 Windows'
    try:
        if not enabled:
            if os.path.isfile(STARTUP_LNK):
                os.remove(STARTUP_LNK)
            return True, '已关闭开机自动启动'
        if not os.path.isfile(VBS_LAUNCHER):
            return False, '找不到启动器：%s' % os.path.basename(VBS_LAUNCHER)
        os.makedirs(STARTUP_DIR, exist_ok=True)
        wscript = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'),
                               'System32', 'wscript.exe')
        script = (
            "$s=(New-Object -ComObject WScript.Shell).CreateShortcut(%s);"
            "$s.TargetPath=%s;$s.Arguments=%s;$s.WorkingDirectory=%s;"
            "$s.Description='Trae 自动签到面板';$s.Save()"
        ) % (_ps_quote(STARTUP_LNK), _ps_quote(wscript),
             _ps_quote('"%s"' % VBS_LAUNCHER), _ps_quote(HERE))
        p = subprocess.run(
            ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', script],
            capture_output=True, timeout=30, creationflags=CREATE_NO_WINDOW)
        if p.returncode != 0 or not os.path.isfile(STARTUP_LNK):
            err = p.stderr.decode('utf-8', 'replace').strip()[:200]
            return False, '创建开机启动快捷方式失败：%s' % (err or '未知原因')
        return True, '已开启开机自动启动（登录后自动打开面板）'
    except Exception as e:
        return False, '设置开机启动失败：%s' % e


# ══════════════════════════════════════════════════════════════
# 自定义 Tab 栏（替代原生 Notebook，便于统一配色）
# ══════════════════════════════════════════════════════════════
class TabBar:
    """自绘 Tab：选中项用主色下划线，全部跟随主题色重绘。"""

    def __init__(self, app, parent):
        self.app = app
        self.bar = app._frame(parent, 'bg')
        self.bar.pack(fill='x', padx=16, pady=(8, 0))
        self.pages = app._frame(parent, 'bg')
        self.pages.pack(fill='both', expand=True)
        self.pages.grid_rowconfigure(0, weight=1)
        self.pages.grid_columnconfigure(0, weight=1)
        self.items = {}
        self.current = None
        app._tabbars.append(self)

    def add(self, key, text, pad=(0, 6)):
        holder = self.app._frame(self.bar, 'bg')
        holder.pack(side='left', padx=pad)
        btn = self.app._button(holder, text, lambda k=key: self.select(k),
                               kind='ghost', font=(UI_FONT, 10))
        btn.pack()
        indicator = self.app._frame(holder, 'bg', height=2)
        indicator.pack(fill='x')
        page = self.app._frame(self.pages, 'bg')
        page.grid(row=0, column=0, sticky='nsew')
        self.items[key] = {'page': page, 'btn': btn, 'ind': indicator,
                           'holder': holder, 'text': text}
        return page

    def select(self, key):
        if key not in self.items:
            return
        self.current = key
        self.items[key]['page'].tkraise()
        self.restyle()

    def restyle(self):
        t = self.app.t
        for key, item in self.items.items():
            selected = key == self.current
            item['btn'].configure(
                bg=t['bg'],
                fg=t['accent'] if selected else t['muted'],
                activebackground=t['bg'],
                activeforeground=t['accent'] if selected else t['muted'],
                font=(UI_FONT, 10, 'bold') if selected else (UI_FONT, 10))
            item['ind'].configure(bg=t['accent'] if selected else t['bg'])

# ══════════════════════════════════════════════════════════════
# 账号编辑对话框
# ══════════════════════════════════════════════════════════════
class AccountDialog(tk.Toplevel):
    fields = (
        ('name', '备注名称', '只影响界面显示，例如：主号'),
        ('uid', 'UID', '可留空；填了用于区分账号与读取签到状态'),
        ('refreshToken', 'refreshToken', '必填（或填 accessToken）。形如 xxx=.18dxxx'),
        ('accessToken', 'accessToken（可留空）', 'eyJ 开头的长 JWT，脚本会自动续期，一般不用填'),
        ('deviceId', '设备号（可留空）', '客户端真实注册号（12~20 位数字或 aha- 开头）'),
    )

    def __init__(self, master, account=None, default_name=''):
        super().__init__(master)
        self.t = master.t
        self.result = None
        self._raw = dict(account or {})
        self.title('编辑账号' if account else '添加账号')
        self.configure(bg=self.t['card'])
        self.resizable(False, False)
        self.transient(master)

        head = tk.Frame(self, bg=self.t['card'])
        head.pack(fill='x', padx=18, pady=(16, 4))
        tk.Label(head, text='账号信息', bg=self.t['card'], fg=self.t['text'],
                 font=(UI_FONT, 12, 'bold')).pack(anchor='w')
        tk.Label(head, text='小号也只填 refreshToken 即可。不知道去哪拿？先登录 Trae 客户端，'
                            '再点面板上的「读取本机账号」。',
                 bg=self.t['card'], fg=self.t['muted'], font=(UI_FONT, 9),
                 justify='left').pack(anchor='w', pady=(2, 0))

        form = tk.Frame(self, bg=self.t['card'])
        form.pack(fill='x', padx=18, pady=10)
        self.vars = {}
        self.entries = {}
        for row, (key, label, hint) in enumerate(self.fields):
            tk.Label(form, text=label, bg=self.t['card'], fg=self.t['text'],
                     font=(UI_FONT, 9)).grid(row=row, column=0, sticky='w', pady=(7, 0))
            if key == 'deviceId':
                value = str(self._raw.get('realDeviceId') or self._raw.get('deviceId') or '')
            else:
                value = str(self._raw.get(key) or '')
            var = tk.StringVar(value=value)
            entry = tk.Entry(form, textvariable=var, width=56, font=(UI_FONT, 9),
                             bg=self.t['input_bg'], fg=self.t['input_fg'],
                             insertbackground=self.t['input_fg'], relief='flat', bd=0,
                             highlightthickness=1, highlightbackground=self.t['input_border'],
                             highlightcolor=self.t['accent'], show='•' if key in
                             ('refreshToken', 'accessToken') else '')
            entry.grid(row=row, column=1, sticky='we', padx=(12, 0), pady=(7, 0))
            tk.Label(form, text=hint, bg=self.t['card'], fg=self.t['muted'],
                     font=(UI_FONT, 8)).grid(row=row, column=2, sticky='w', padx=(10, 0))
            self.vars[key] = var
            self.entries[key] = entry
        form.columnconfigure(1, weight=1)

        self.var_show = tk.BooleanVar(value=False)
        tk.Checkbutton(self, text='显示凭据', variable=self.var_show, bg=self.t['card'],
                       fg=self.t['text'], activebackground=self.t['card'],
                       activeforeground=self.t['text'], selectcolor=self.t['input_bg'],
                       font=(UI_FONT, 9), bd=0, highlightthickness=0,
                       command=self._toggle_show).pack(anchor='w', padx=18, pady=(4, 6))

        bind = tk.Frame(self, bg=self.t['card'])
        bind.pack(fill='x', padx=18, pady=(2, 8))
        tk.Label(bind, text='绑定客户端', bg=self.t['card'], fg=self.t['text'],
                 font=(UI_FONT, 9)).pack(side='left')
        clients = ['自动'] + (SWITCH.known_clients() if SWITCH else [])
        self.var_client = tk.StringVar(value=str(self._raw.get('client') or '自动'))
        ttk.Combobox(bind, textvariable=self.var_client, values=clients, width=16,
                     state='readonly', font=(UI_FONT, 9)).pack(side='left', padx=(8, 10))
        self._dialog_button(bind, '读取该号的设备密钥', self._read_local_keys).pack(side='left')
        self.var_bind_hint = tk.StringVar(
            value='已带密钥' if self._raw.get('deviceKeyPem') else '缺密钥，点左侧按钮补齐')
        tk.Label(bind, textvariable=self.var_bind_hint, bg=self.t['card'],
                 fg=self.t['muted'], font=(UI_FONT, 8)).pack(side='left', padx=8)

        tk.Label(self, text='提示：同一台电脑上的真实设备号一天只能签一个账号；'
                            '多账号请分别登录各自客户端后「读取本机账号」。',
                 bg=self.t['card'], fg=self.t['muted'], font=(UI_FONT, 8),
                 wraplength=600, justify='left').pack(anchor='w', padx=18, pady=(0, 12))

        bar = tk.Frame(self, bg=self.t['card'])
        bar.pack(fill='x', padx=18, pady=(0, 16))
        self._dialog_button(bar, '取消', self.destroy, primary=False).pack(side='right')
        self._dialog_button(bar, '保存', self._on_ok, primary=True).pack(side='right', padx=(0, 10))

        self._default_name = default_name
        self._center(master)
        self.grab_set()
        self.bind('<Return>', lambda e: self._on_ok())
        self.bind('<Escape>', lambda e: self.destroy())
        self.entries['refreshToken'].focus_set()
        self.wait_window(self)

    def _dialog_button(self, parent, text, command, primary=False):
        bg = self.t['accent'] if primary else self.t['btn_bg']
        fg = self.t['accent_fg'] if primary else self.t['btn_fg']
        hover = self.t['accent_hover'] if primary else self.t['btn_hover']
        return tk.Button(parent, text=text, command=command, bg=bg, fg=fg,
                         activebackground=hover, activeforeground=fg, relief='flat',
                         bd=0, padx=16, pady=6, cursor='hand2',
                         font=(UI_FONT, 9), highlightthickness=0)

    def _toggle_show(self):
        show = '' if self.var_show.get() else '•'
        for key in ('refreshToken', 'accessToken'):
            self.entries[key].configure(show=show)

    def _read_local_keys(self):
        """按 uid / 设备号在绑定的客户端里找回这套设备密钥。"""
        if SWITCH is None:
            self.var_bind_hint.set('缺少 trae_switch.py')
            return
        client = '' if self.var_client.get() == '自动' else self.var_client.get()
        info = SWITCH.bind_info({'uid': self.vars['uid'].get().strip(),
                                 'realDeviceId': self.vars['deviceId'].get().strip(),
                                 'client': client})
        if not info.get('privateKeyPem'):
            self.var_bind_hint.set('该客户端里没有可用密钥')
            return
        self._raw['deviceKeyPem'] = info['privateKeyPem']
        self._raw['devicePubPem'] = info['publicKeyPem']
        if info.get('machineId'):
            self._raw['machineId'] = info['machineId']
        if info.get('deviceId'):
            self.vars['deviceId'].set(info['deviceId'])
        self.var_bind_hint.set('已读取：%s' % info['deviceId'])

    def _center(self, master):
        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        x = master.winfo_rootx() + (master.winfo_width() - w) // 2
        y = master.winfo_rooty() + (master.winfo_height() - h) // 3
        self.geometry('+%d+%d' % (max(x, 0), max(y, 0)))

    def _on_ok(self):
        vals = {k: v.get().strip() for k, v in self.vars.items()}
        if not vals['refreshToken'] and not vals['accessToken']:
            messagebox.showwarning('缺少凭据', 'refreshToken 和 accessToken 至少要填一个。', parent=self)
            return
        if vals['refreshToken'].startswith('eyJ'):
            if not messagebox.askyesno(
                    '确认凭据类型',
                    'refreshToken 看起来像 accessToken（eyJ 开头）。\n'
                    '两者填反会导致「鉴权失败」，仍然保存吗？', parent=self):
                return
        if vals['accessToken'] and not vals['accessToken'].startswith('eyJ'):
            if not messagebox.askyesno(
                    '确认凭据类型',
                    'accessToken 通常以 eyJ 开头，当前值可能不是 accessToken。\n仍然保存吗？',
                    parent=self):
                return
        did = vals['deviceId']
        if did and not DEVICE_ID_RE.match(did):
            messagebox.showwarning(
                '设备号格式不对',
                '设备号应是客户端注册的标识：12~20 位数字，或 aha- 开头的十六进制。\n'
                '不确定就留空，让面板自动读取真实设备号。', parent=self)
            return

        acc = dict(self._raw)
        old_did = str(acc.get('realDeviceId') or acc.get('deviceId') or '')
        client = self.var_client.get().strip()
        if client and client != '自动':
            acc['client'] = client
        else:
            acc.pop('client', None)
        acc['name'] = vals['name'] or self._default_name
        acc['uid'] = vals['uid']
        acc['refreshToken'] = vals['refreshToken']
        acc['accessToken'] = vals['accessToken']
        if did != old_did:
            acc['realDeviceId'] = did
            acc.pop('deviceId', None)
        if not acc.get('uid') and acc.get('accessToken'):
            acc['uid'] = jwt_uid(acc['accessToken'])
        for key in ('uid', 'accessToken', 'refreshToken', 'realDeviceId'):
            if not str(acc.get(key) or '').strip():
                acc.pop(key, None)
        acc['enabled'] = bool(acc.get('enabled', True))
        self.result = acc
        self.destroy()


# ══════════════════════════════════════════════════════════════
# 添加账号：先选入口（手动 / 浏览器）
# ══════════════════════════════════════════════════════════════
class AddAccountDialog(tk.Toplevel):
    """两个入口：手动填凭据，或直接打开浏览器授权登录后自动入库。"""

    def __init__(self, master):
        super().__init__(master)
        self.t = master.t
        self.result = None
        self.title('添加账号')
        self.configure(bg=self.t['card'])
        self.resizable(False, False)
        self.transient(master)

        tk.Label(self, text='选择添加方式', bg=self.t['card'], fg=self.t['text'],
                 font=(UI_FONT, 12, 'bold')).pack(anchor='w', padx=20, pady=(16, 2))
        tk.Label(self, text='从浏览器获取：打开官网授权页，用手机号 + 验证码登录，成功后自动入库。\n'
                            '手动填写：粘贴别处导出的 refreshToken。',
                 bg=self.t['card'], fg=self.t['muted'], font=(UI_FONT, 9),
                 justify='left').pack(anchor='w', padx=20, pady=(0, 14))

        row = tk.Frame(self, bg=self.t['card'])
        row.pack(fill='x', padx=20)
        self._big_button(row, '从浏览器获取', '（推荐）', lambda: self._pick('web'),
                         primary=True).pack(side='left', fill='x', expand=True)
        self._big_button(row, '手动填写', '（粘贴凭据）', lambda: self._pick('manual'),
                         primary=False).pack(side='left', fill='x', expand=True, padx=(10, 0))

        bar = tk.Frame(self, bg=self.t['card'])
        bar.pack(fill='x', padx=20, pady=(14, 16))
        tk.Button(bar, text='取消', command=self.destroy, bg=self.t['btn_bg'],
                  fg=self.t['btn_fg'], activebackground=self.t['btn_hover'],
                  activeforeground=self.t['btn_fg'], relief='flat', bd=0, padx=16, pady=5,
                  cursor='hand2', font=(UI_FONT, 9), highlightthickness=0).pack(side='right')

        self._center(master)
        self.grab_set()
        self.bind('<Escape>', lambda e: self.destroy())
        self.wait_window(self)

    def _big_button(self, parent, text, hint, command, primary):
        bg = self.t['accent'] if primary else self.t['btn_bg']
        fg = self.t['accent_fg'] if primary else self.t['btn_fg']
        hover = self.t['accent_hover'] if primary else self.t['btn_hover']
        btn = tk.Button(parent, command=command, bg=bg, fg=fg, activebackground=hover,
                        activeforeground=fg, relief='flat', bd=0, padx=14, pady=10,
                        cursor='hand2', font=(UI_FONT, 10), highlightthickness=0)
        btn.configure(text='%s\n%s' % (text, hint), justify='center')
        return btn

    def _pick(self, value):
        self.result = value
        self.destroy()

    def _center(self, master):
        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        x = master.winfo_rootx() + (master.winfo_width() - w) // 2
        y = master.winfo_rooty() + (master.winfo_height() - h) // 3
        self.geometry('+%d+%d' % (max(x, 0), max(y, 0)))


# ══════════════════════════════════════════════════════════════
# 浏览器授权进度窗口
# ══════════════════════════════════════════════════════════════
class WebAuthDialog(tk.Toplevel):
    """等浏览器授权时的进度窗：授权成功/失败/取消都会立刻关掉它。"""

    def __init__(self, master, on_cancel=None, on_reopen=None):
        super().__init__(master)
        self.t = master.t
        self._on_cancel = on_cancel
        self._on_reopen = on_reopen
        self._job = None
        self._start = time.time()
        self.title('正在授权')
        self.configure(bg=self.t['card'])
        self.resizable(False, False)
        self.transient(master)

        box = tk.Frame(self, bg=self.t['card'])
        box.pack(fill='both', expand=True, padx=22, pady=18)
        tk.Label(box, text='请在浏览器里完成登录', bg=self.t['card'], fg=self.t['text'],
                 font=(UI_FONT, 12, 'bold')).pack(anchor='w')
        tk.Label(box, text='已打开 Trae 官网授权页：输入手机号 → 获取验证码 → 登录。\n'
                           '检测到授权完成后，本窗口会自动关闭并把账号加进列表。',
                 bg=self.t['card'], fg=self.t['muted'], font=(UI_FONT, 9),
                 justify='left').pack(anchor='w', pady=(6, 10))

        self.var_status = tk.StringVar(value='等待授权…')
        self.lab_status = tk.Label(box, textvariable=self.var_status, bg=self.t['card'],
                                   fg=self.t['accent'], font=(UI_FONT, 10))
        self.lab_status.pack(anchor='w')

        self.var_elapsed = tk.StringVar(value='已等待 0 秒')
        tk.Label(box, textvariable=self.var_elapsed, bg=self.t['card'], fg=self.t['muted'],
                 font=(UI_FONT, 8)).pack(anchor='w', pady=(2, 12))

        bar = tk.Frame(box, bg=self.t['card'])
        bar.pack(fill='x')
        tk.Button(bar, text='重新打开浏览器', command=self._reopen, bg=self.t['btn_bg'],
                  fg=self.t['btn_fg'], activebackground=self.t['btn_hover'],
                  activeforeground=self.t['btn_fg'], relief='flat', bd=0, padx=14, pady=6,
                  cursor='hand2', font=(UI_FONT, 9), highlightthickness=0).pack(side='left')
        tk.Button(bar, text='取消授权', command=self._cancel, bg=self.t['btn_bg'],
                  fg=self.t['err'], activebackground=self.t['danger_hover'],
                  activeforeground=self.t['err'], relief='flat', bd=0, padx=14, pady=6,
                  cursor='hand2', font=(UI_FONT, 9), highlightthickness=0).pack(side='right')

        self.protocol('WM_DELETE_WINDOW', self._cancel)
        self._center(master)
        self.lift()
        self._tick()

    def _center(self, master):
        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        x = master.winfo_rootx() + (master.winfo_width() - w) // 2
        y = master.winfo_rooty() + (master.winfo_height() - h) // 3
        self.geometry('+%d+%d' % (max(x, 0), max(y, 0)))

    def _tick(self):
        if not self.winfo_exists():
            return
        self.var_elapsed.set('已等待 %d 秒' % int(time.time() - self._start))
        self._job = self.after(1000, self._tick)

    def set_status(self, text, level='accent'):
        if not self.winfo_exists():
            return
        self.var_status.set(text)
        self.lab_status.configure(fg=self.t.get(level, self.t['accent']))

    def _reopen(self):
        if self._on_reopen:
            try:
                self._on_reopen()
            except Exception:                                # noqa: BLE001
                pass

    def _cancel(self):
        self.set_status('正在取消…', 'warn')
        if self._on_cancel:
            try:
                self._on_cancel()
            except Exception:                                # noqa: BLE001
                pass

    def close(self):
        if self._job:
            try:
                self.after_cancel(self._job)
            except Exception:                                # noqa: BLE001
                pass
            self._job = None
        if self.winfo_exists():
            self.destroy()


# ══════════════════════════════════════════════════════════════
# 主界面
# ══════════════════════════════════════════════════════════════
class App(tk.Tk):
    def __init__(self, selftest=False):
        super().__init__()
        self.selftest = selftest
        self.cfg = self.load_config()
        self.theme_name = str((self.cfg.get('ui') or {}).get('theme') or 'light')
        if self.theme_name not in THEMES:
            self.theme_name = 'light'
        self.t = THEMES[self.theme_name]
        self._stylers = []
        self._tabbars = []

        self.running = False
        self.extracting = False
        self.web_running = False
        self.web_stop = threading.Event()
        self.web_dialog = None
        self._web_url = ''
        self.switching = False
        self.msgq = queue.Queue()
        self.retry_at = None
        self.retry_count = 0
        self.retry_date = today_str()
        self._run_interactive = False
        self._pending_run = None
        self._auto_fail_at = None
        self._credits_pending = False

        self.title('%s %s' % (APP_TITLE, APP_VERSION))
        self.geometry('1100x780')
        self.minsize(1000, 720)
        self.configure(bg=self.t['bg'])
        self.protocol('WM_DELETE_WINDOW', self._on_close)
        self.report_callback_exception = self._on_tk_error

        self._init_style()
        self._build_header()
        self._build_action_bar()
        self._build_tabs()
        self._build_footer()
        self._apply_config_to_widgets()
        self.apply_theme(self.theme_name, save=False)

        self.bind('<Control-s>', lambda e: self.save_config())
        self.bind('<F5>', lambda e: self.start_run('手动'))

        self.log_ui('面板已启动，配置文件：%s' % CONFIG_FILE, 'dim')
        self.refresh_status()
        self.refresh_live_account()
        self.after(300, self._auto_bind_accounts)
        self.after(1500, self._refresh_credits)
        self.after(120, self._pump)
        self.after(1000, self._tick)
        if not selftest:
            if not self.cfg.get('accounts'):
                self.after(900, lambda: self.extract_accounts(auto=True))
            elif self._needs_device_sync():
                self.after(1200, lambda: self.extract_accounts(auto=True))

    # ---------- 主题 ----------
    def _reg(self, widget, apply_fn):
        self._stylers.append((widget, apply_fn))
        try:
            apply_fn(self.t)
        except tk.TclError:
            pass

    def _frame(self, parent, role='bg', **kw):
        frame = tk.Frame(parent, bg=self.t[role], **kw)
        self._reg(frame, lambda t, w=frame, r=role: w.configure(bg=t[r]))
        return frame

    def _card(self, parent):
        frame = tk.Frame(parent, bg=self.t['card'], bd=0,
                         highlightthickness=1,
                         highlightbackground=self.t['border'])
        self._reg(frame, lambda t, w=frame: w.configure(
            bg=t['card'], highlightbackground=t['border']))
        return frame

    def _label(self, parent, text=None, fg='text', bg=None, font=None, **kw):
        widget = tk.Label(parent, text=text, fg=self.t[fg],
                          bg=self.t[bg] if bg else parent.cget('bg'),
                          font=font or (UI_FONT, 10), **kw)

        def apply(t, w=widget, fk=fg, bk=bg, p=parent):
            try:
                w.configure(fg=t[fk], bg=t[bk] if bk else p.cget('bg'))
            except tk.TclError:
                pass
        self._reg(widget, apply)
        return widget

    def _check(self, parent, text, variable, command=None, font=None):
        widget = tk.Checkbutton(
            parent, text=text, variable=variable, command=command, anchor='w',
            bd=0, highlightthickness=0, font=font or (UI_FONT, 9),
            fg=self.t['text'], bg=parent.cget('bg'),
            activeforeground=self.t['text'], activebackground=parent.cget('bg'),
            selectcolor=self.t['input_bg'], cursor='hand2')

        def apply(t, w=widget, p=parent):
            try:
                w.configure(fg=t['text'], bg=p.cget('bg'),
                            activeforeground=t['text'], activebackground=p.cget('bg'),
                            selectcolor=t['input_bg'], disabledforeground=t['off'])
            except tk.TclError:
                pass
        self._reg(widget, apply)
        return widget

    def _button(self, parent, text, command, kind='normal', textvar=None, font=None):
        kinds = {
            'primary': ('accent', 'accent_fg', 'accent_hover'),
            'normal': ('btn_bg', 'btn_fg', 'btn_hover'),
            'danger': ('btn_bg', 'err', 'danger_hover'),
            'header': ('chip_bg', 'chip_fg', 'chip_hover'),
            'ghost': ('bg', 'muted', 'btn_hover'),
        }
        bg_key, fg_key, hover_key = kinds[kind]
        widget = tk.Button(
            parent, command=command, relief='flat', bd=0, padx=14, pady=6,
            cursor='hand2', font=font or (UI_FONT, 9), highlightthickness=0,
            bg=self.t[bg_key], fg=self.t[fg_key],
            activebackground=self.t[hover_key], activeforeground=self.t[fg_key])
        if textvar is not None:
            widget.configure(textvariable=textvar)
        else:
            widget.configure(text=text)
        state = {'hover': False}

        def on_enter(_e, w=widget, hk=hover_key):
            state['hover'] = True
            w.configure(bg=self.t[hk])

        def on_leave(_e, w=widget, bk=bg_key):
            state['hover'] = False
            w.configure(bg=self.t[bk])

        widget.bind('<Enter>', on_enter)
        widget.bind('<Leave>', on_leave)

        def apply(t, w=widget, bk=bg_key, fk=fg_key, hk=hover_key, s=state):
            try:
                w.configure(bg=t[hk] if s['hover'] else t[bk], fg=t[fk],
                            activeforeground=t[fk], activebackground=t[hk])
            except tk.TclError:
                pass
        self._reg(widget, apply)
        return widget

    def _entry(self, parent, variable, width=24, show=''):
        widget = tk.Entry(
            parent, textvariable=variable, width=width, show=show,
            font=(UI_FONT, 9), relief='flat', bd=0, highlightthickness=1,
            bg=self.t['input_bg'], fg=self.t['input_fg'],
            insertbackground=self.t['input_fg'],
            highlightbackground=self.t['input_border'],
            highlightcolor=self.t['accent'])

        def apply(t, w=widget):
            try:
                w.configure(bg=t['input_bg'], fg=t['input_fg'],
                            insertbackground=t['input_fg'],
                            highlightbackground=t['input_border'],
                            highlightcolor=t['accent'])
            except tk.TclError:
                pass
        self._reg(widget, apply)
        return widget

    def _text(self, parent, kind='input', height=3, wrap='char', font=None):
        if kind == 'log':
            bg_key, fg_key = 'log_bg', 'log_fg'
            highlight = 0
        else:
            bg_key, fg_key = 'input_bg', 'input_fg'
            highlight = 1
        widget = tk.Text(
            parent, height=height, wrap=wrap, font=font or (UI_FONT, 9),
            bg=self.t[bg_key], fg=self.t[fg_key],
            insertbackground=self.t[fg_key], relief='flat', bd=0,
            highlightthickness=highlight,
            highlightbackground=self.t['input_border'],
            highlightcolor=self.t['accent'])

        def apply(t, w=widget, bk=bg_key, fk=fg_key, hl=highlight):
            try:
                w.configure(bg=t[bk], fg=t[fk], insertbackground=t[fk])
                if hl:
                    w.configure(highlightbackground=t['input_border'],
                                highlightcolor=t['accent'])
            except tk.TclError:
                pass
        self._reg(widget, apply)
        return widget

    def _init_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use('clam')
        except Exception:
            pass
        self._ttk = style

    def _style_ttk(self):
        t = self.t
        style = self._ttk
        try:
            style.theme_use('clam')
        except Exception:
            pass
        style.configure('Treeview', background=t['card'], fieldbackground=t['card'],
                        foreground=t['text'], borderwidth=0, rowheight=32,
                        font=(UI_FONT, 9))
        style.configure('Treeview.Heading', background=t['tree_head'],
                        foreground=t['muted'], relief='flat', borderwidth=0,
                        padding=(6, 9), font=(UI_FONT, 9, 'bold'))
        style.map('Treeview',
                  background=[('selected', t['tree_sel'])],
                  foreground=[('selected', t['text'])])
        style.map('Treeview.Heading', background=[('active', t['tree_head'])])
        style.configure('Vertical.TScrollbar', background=t['border'],
                        troughcolor=t['card'], bordercolor=t['card'],
                        arrowcolor=t['muted'], width=10,
                        darkcolor=t['card'], lightcolor=t['card'])
        style.configure('Horizontal.TScrollbar', background=t['border'],
                        troughcolor=t['card'], bordercolor=t['card'],
                        arrowcolor=t['muted'],
                        darkcolor=t['card'], lightcolor=t['card'])

    def apply_theme(self, name, save=True):
        if name not in THEMES:
            name = 'light'
        self.theme_name = name
        self.t = THEMES[name]
        self.configure(bg=self.t['bg'])
        self._style_ttk()
        for widget, apply_fn in self._stylers:
            if not widget.winfo_exists():
                continue
            try:
                apply_fn(self.t)
            except tk.TclError:
                pass
        for tabbar in self._tabbars:
            tabbar.restyle()
        if hasattr(self, 'tree'):
            self._style_tree_tags()
        if hasattr(self, 'log_text'):
            for tag, key in (('ok', 'ok'), ('err', 'err'), ('warn', 'warn'),
                             ('dim', 'muted'), ('head', 'accent')):
                self.log_text.tag_configure(tag, foreground=self.t[key])
        if hasattr(self, 'var_theme_btn'):
            self.var_theme_btn.set('深色模式' if name == 'light' else '浅色模式')
        if hasattr(self, 'row_menu'):
            try:
                self.row_menu.configure(bg=self.t['card'], fg=self.t['text'],
                                        activebackground=self.t['accent'],
                                        activeforeground=self.t['accent_fg'])
            except tk.TclError:
                pass
        if hasattr(self, 'var_theme_now'):
            self.var_theme_now.set('当前：%s' % ('浅色' if name == 'light' else '深色'))
        if save:
            self.cfg.setdefault('ui', {})['theme'] = name
            self.save_config(silent=True)
            self.var_footer.set('已切换到%s主题' % ('浅色' if name == 'light' else '深色'))

    def toggle_theme(self):
        self.apply_theme('dark' if self.theme_name == 'light' else 'light')

    def _style_tree_tags(self):
        t = self.t
        for tag, key in (('done', 'ok'), ('pending', 'warn'),
                         ('fail', 'err'), ('off', 'off')):
            self.tree.tag_configure(tag, foreground=t[key])
        self.tree.tag_configure('row_even', background=t['card'])
        self.tree.tag_configure('row_odd', background=t['row_alt'])

    # ---------- 头部 ----------
    def _build_header(self):
        header = self._frame(self, 'header_bg', height=76)
        header.pack(fill='x')
        header.pack_propagate(False)

        left = self._frame(header, 'header_bg')
        left.pack(side='left', padx=20, pady=12)
        title_row = self._frame(left, 'header_bg')
        title_row.pack(anchor='w')
        dot = self._frame(title_row, 'accent', width=10, height=10)
        dot.pack(side='left', padx=(0, 8))
        dot.pack_propagate(False)
        self._label(title_row, APP_TITLE, fg='header_fg', bg='header_bg',
                    font=(UI_FONT, 16, 'bold')).pack(side='left')
        self._label(left, '多账号 · 每日定时签到 · 失败自动补签',
                    fg='header_sub', bg='header_bg', font=(UI_FONT, 9)).pack(
            anchor='w', pady=(2, 0))

        right = self._frame(header, 'header_bg')
        right.pack(side='right', padx=20)
        self.var_chip_status = tk.StringVar(value='今日 0/0 已签到')
        self.var_chip_next = tk.StringVar(value='下次自动：--:--')
        for var in (self.var_chip_status, self.var_chip_next):
            self._label(right, textvariable=var, fg='chip_fg', bg='chip_bg',
                        font=(UI_FONT, 9), padx=12, pady=6).pack(side='left', padx=(8, 0))
        self.var_theme_btn = tk.StringVar(value='深色模式')
        self._button(right, '', self.toggle_theme, kind='header',
                     textvar=self.var_theme_btn).pack(side='left', padx=(10, 0))

    # ---------- 顶部操作栏 ----------
    def _build_action_bar(self):
        bar = self._frame(self, 'card', height=56)
        bar.pack(fill='x')
        bar.pack_propagate(False)
        left = self._frame(bar, 'card')
        left.pack(side='left', padx=16, pady=10)
        self._button(left, '读取本机账号', lambda: self.extract_accounts(False),
                     kind='primary').pack(side='left')
        self._button(left, '添加账号', self.add_account).pack(side='left', padx=(8, 0))
        self._button(left, '编辑', self.edit_account).pack(side='left', padx=(8, 0))
        self._button(left, '重命名', self.rename_account).pack(side='left', padx=(8, 0))
        self._button(left, '启用/停用', self.toggle_account).pack(side='left', padx=(8, 0))
        self._button(left, '刷新', self.refresh_list).pack(side='left', padx=(8, 0))
        self._button(left, '删除', self.delete_account, kind='danger').pack(
            side='left', padx=(8, 0))

        right = self._frame(bar, 'card')
        right.pack(side='right', padx=16, pady=10)
        self.var_show_tokens = tk.BooleanVar(value=False)
        self._check(right, '显示凭据', self.var_show_tokens,
                    command=self.refresh_status).pack(side='left', padx=(0, 12))
        self._button(right, '保存配置', lambda: self.save_config()).pack(side='left')
        self.btn_run = self._button(right, '立即签到', lambda: self.start_run('手动'),
                                    kind='primary')
        self.btn_run.pack(side='left', padx=(10, 0))

    # ---------- 主体 Tab ----------
    def _build_tabs(self):
        holder = self._frame(self, 'bg')
        holder.pack(fill='both', expand=True)
        self.main_tabs = TabBar(self, holder)
        self.page_accounts = self.main_tabs.add('accounts', '账号列表')
        self.page_settings = self.main_tabs.add('settings', '设置')
        self.page_log = self.main_tabs.add('log', '运行日志')
        self._build_accounts_page(self.page_accounts)
        self._build_settings_page(self.page_settings)
        self._build_log_page(self.page_log)
        self.main_tabs.select('accounts')

    # ---------- 账号列表页 ----------
    def _build_accounts_page(self, page):
        wrap = self._card(page)
        wrap.pack(fill='both', expand=True, padx=16, pady=(10, 4))

        columns = ('state', 'name', 'uid', 'token', 'device', 'bind', 'credits')
        self.tree = ttk.Treeview(wrap, columns=columns, show='headings',
                                 selectmode='browse')
        headings = {'state': ('今日状态', 110), 'name': ('备注名称', 170),
                    'uid': ('UID', 140), 'token': ('refreshToken', 170),
                    'device': ('设备号', 170), 'bind': ('绑定客户端', 140),
                    'credits': ('签到积分', 95)}
        for col in columns:
            self.tree.heading(col, text=headings[col][0])
            self.tree.column(col, width=headings[col][1],
                             anchor='center' if col in ('state', 'credits') else 'w')
        vsb = ttk.Scrollbar(wrap, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side='left', fill='both', expand=True)
        vsb.pack(side='right', fill='y')
        self.tree.bind('<Double-1>', lambda e: self.edit_account())
        self.tree.bind('<Button-3>', self._show_row_menu)
        self._build_row_menu()
        self._style_tree_tags()

        self.empty_hint = self._label(
            wrap, '还没有账号\n点上方「读取本机账号」自动读取，或手动添加',
            fg='muted', bg='card', font=(UI_FONT, 10), justify='center')
        self.empty_hint.place(relx=0.5, rely=0.5, anchor='center')

        bottom = self._frame(page, 'bg')
        bottom.pack(fill='x', padx=16, pady=(2, 8))
        self.var_account_count = tk.StringVar(value='共 0 个账号')
        self._label(bottom, textvariable=self.var_account_count, fg='muted',
                    font=(UI_FONT, 9)).pack(side='right')
        self.btn_switch = self._button(bottom, '切换到此账号', self.switch_to_selected)
        self.btn_switch.pack(side='right', padx=(0, 12))
        self.var_live_account = tk.StringVar(value='')
        self._label(bottom, textvariable=self.var_live_account, fg='muted',
                    font=(UI_FONT, 8)).pack(side='right', padx=(0, 10))
        self._label(bottom, '小提示：每个账号必须对应它自己的客户端设备号；'
                            '缺设备号时点「读取本机账号」自动同步。双击一行可编辑。',
                    fg='muted', font=(UI_FONT, 8), justify='left').pack(side='left')

    # ---------- 设置页 ----------
    def _build_settings_page(self, page):
        self.settings_tabs = TabBar(self, page)
        p1 = self.settings_tabs.add('schedule', '定时与补签')
        p2 = self.settings_tabs.add('notify', '推送与设备')
        p3 = self.settings_tabs.add('backup', '配置与外观')
        self._build_schedule_page(p1)
        self._build_notify_page(p2)
        self._build_backup_page(p3)
        self.settings_tabs.select('schedule')

    def _section(self, parent, title):
        card = self._card(parent)
        card.pack(fill='x', padx=2, pady=(0, 12))
        head = self._frame(card, 'card')
        head.pack(fill='x', padx=16, pady=(12, 0))
        mark = self._frame(head, 'accent', width=4, height=16)
        mark.pack(side='left', padx=(0, 8))
        mark.pack_propagate(False)
        self._label(head, title, fg='text', bg='card',
                    font=(UI_FONT, 11, 'bold')).pack(side='left')
        body = self._frame(card, 'card')
        body.pack(fill='x', padx=16, pady=(8, 12))
        return body

    def _build_schedule_page(self, page):
        body = self._section(page, '定时签到')
        row = self._frame(body, 'card')
        row.pack(fill='x', pady=4)
        self._label(row, '每天', bg='card').pack(side='left')
        self.var_time = tk.StringVar(value='08:00')
        self._entry(row, self.var_time, width=8).pack(side='left', padx=6)
        self._label(row, '自动签到（北京时间）', bg='card').pack(side='left')
        self._label(row, '建议保持 08:00', fg='muted', bg='card',
                    font=(UI_FONT, 8)).pack(side='left', padx=10)

        row2 = self._frame(body, 'card')
        row2.pack(fill='x', pady=(8, 0))
        self.var_startup = tk.BooleanVar(value=startup_enabled())
        self.chk_startup = self._check(
            row2, '开机自动启动面板（推荐，登录后自动打开）',
            self.var_startup, command=self._toggle_startup)
        self.chk_startup.pack(side='left')
        if os.name != 'nt':
            self.chk_startup.configure(state='disabled')

        row3 = self._frame(body, 'card')
        row3.pack(fill='x', pady=(6, 0))
        self.var_catchup = tk.BooleanVar(value=True)
        self._check(row3, '打开面板时补签今天还没签的账号',
                    self.var_catchup).pack(side='left')

        body = self._section(page, '失败自动补签')
        row = self._frame(body, 'card')
        row.pack(fill='x', pady=4)
        self.var_retry = tk.BooleanVar(value=True)
        self._check(row, '启用', self.var_retry).pack(side='left')
        self._label(row, '每', bg='card').pack(side='left', padx=(12, 4))
        self.var_retry_min = tk.StringVar(value='60')
        self._entry(row, self.var_retry_min, width=6).pack(side='left')
        self._label(row, '分钟重试，最多', bg='card').pack(side='left', padx=(4, 4))
        self.var_retry_max = tk.StringVar(value='6')
        self._entry(row, self.var_retry_max, width=4).pack(side='left')
        self._label(row, '次，截止', bg='card').pack(side='left', padx=(4, 4))
        self.var_deadline = tk.StringVar(value='23:30')
        self._entry(row, self.var_deadline, width=8).pack(side='left')
        self._label(body, '命中 9074 的账号会被脚本冷却约 55 分钟，60 分钟重试一次刚好衔接；'
                          '全天多次补签可显著提高成功率。',
                    fg='muted', bg='card', font=(UI_FONT, 8),
                    justify='left').pack(anchor='w', pady=(10, 0))

    def _build_notify_page(self, page):
        body = self._section(page, '推送通知（可选）')
        row = self._frame(body, 'card')
        row.pack(fill='x', pady=4)
        self._label(row, '企业微信机器人 key', bg='card').pack(side='left')
        self.var_qywx = tk.StringVar()
        self._entry(row, self.var_qywx, width=40, show='•').pack(side='left', padx=8)
        self._label(row, 'webhook 地址 ?key= 后面那段', fg='muted', bg='card',
                    font=(UI_FONT, 8)).pack(side='left')
        row = self._frame(body, 'card')
        row.pack(fill='x', pady=(8, 0))
        self._label(row, 'PushPlus token', bg='card').pack(side='left')
        self.var_plusplus = tk.StringVar()
        self._entry(row, self.var_plusplus, width=40, show='•').pack(side='left', padx=8)
        self._label(row, '可不填，二选一', fg='muted', bg='card',
                    font=(UI_FONT, 8)).pack(side='left')

        body = self._section(page, '设备密钥（新版续期必需）')
        top = self._frame(body, 'card')
        top.pack(fill='x', pady=(0, 6))
        self._button(top, '读取本机设备密钥', self.extract_keys).pack(side='left')
        self._label(top, '换机 / 续期失败（20405）时点这里，其余情况保持默认',
                    fg='muted', bg='card', font=(UI_FONT, 8)).pack(side='left', padx=10)

        grid = self._frame(body, 'card')
        grid.pack(fill='x')
        self.var_dev_mid = tk.StringVar()
        self.var_dev_did = tk.StringVar()
        self.var_dev_name = tk.StringVar()
        self.var_dev_model = tk.StringVar()
        self.var_dev_brand = tk.StringVar()
        line1 = self._frame(grid, 'card')
        line1.pack(fill='x', pady=3)
        self._label(line1, 'machineId', bg='card').pack(side='left')
        self._entry(line1, self.var_dev_mid, width=24).pack(side='left', padx=(6, 16))
        self._label(line1, '设备号', bg='card').pack(side='left')
        self._entry(line1, self.var_dev_did, width=20).pack(side='left', padx=6)

        line2 = self._frame(grid, 'card')
        line2.pack(fill='x', pady=3)
        self._label(line2, '设备名', bg='card').pack(side='left')
        self._entry(line2, self.var_dev_name, width=20).pack(side='left', padx=(6, 16))
        self._label(line2, '型号', bg='card').pack(side='left')
        self._entry(line2, self.var_dev_model, width=16).pack(side='left', padx=(6, 16))
        self._label(line2, '品牌', bg='card').pack(side='left')
        self._entry(line2, self.var_dev_brand, width=14).pack(side='left', padx=6)

        for label, attr in (('设备私钥（PEM）', 'txt_dev_key'),
                            ('设备公钥（PEM）', 'txt_dev_pub')):
            self._label(grid, label, bg='card').pack(anchor='w', pady=(8, 2))
            txt = self._text(grid, kind='input', height=2, wrap='char',
                             font=('Consolas', 8))
            txt.pack(fill='x')
            setattr(self, attr, txt)

    def _build_backup_page(self, page):
        body = self._section(page, '外观')
        row = self._frame(body, 'card')
        row.pack(fill='x')
        self.var_theme_now = tk.StringVar(value='当前：浅色')
        self._label(row, textvariable=self.var_theme_now, bg='card').pack(side='left')
        self._button(row, '浅色', lambda: self.apply_theme('light')).pack(
            side='left', padx=(16, 6))
        self._button(row, '深色', lambda: self.apply_theme('dark')).pack(side='left')
        self._label(body, '也可以在右上角一键切换，选择会记住。',
                    fg='muted', bg='card', font=(UI_FONT, 8)).pack(anchor='w', pady=(8, 0))

        body = self._section(page, '配置备份')
        self._label(body, '导出的文件包含账号 token、设备密钥等敏感信息，'
                          '请只保存在自己的电脑上。',
                    fg='muted', bg='card', font=(UI_FONT, 9),
                    justify='left').pack(anchor='w')
        row = self._frame(body, 'card')
        row.pack(fill='x', pady=(10, 4))
        self._button(row, '导出配置…', self.export_config).pack(side='left')
        self._button(row, '导入配置…', self.import_config).pack(side='left', padx=8)
        self._button(row, '打开配置文件夹', self.open_config_dir).pack(side='left', padx=8)
        self._label(body, '配置文件：%s' % CONFIG_FILE, fg='muted', bg='card',
                    font=(UI_FONT, 8), wraplength=780,
                    justify='left').pack(anchor='w', pady=(10, 0))
        self._label(body, '签到状态文件：%s' % STATE_FILE, fg='muted', bg='card',
                    font=(UI_FONT, 8), wraplength=780,
                    justify='left').pack(anchor='w', pady=(4, 0))

    # ---------- 日志页 ----------
    def _build_log_page(self, page):
        toolbar = self._frame(page, 'bg')
        toolbar.pack(fill='x', padx=16, pady=(10, 6))
        self._button(toolbar, '清空显示', self.clear_log).pack(side='left')
        self._button(toolbar, '打开日志文件夹', self.open_log_dir).pack(side='left', padx=8)
        self.var_autoscroll = tk.BooleanVar(value=True)
        self._check(toolbar, '自动滚动', self.var_autoscroll).pack(side='right')

        wrap = self._frame(page, 'log_bg')
        wrap.pack(fill='both', expand=True, padx=16, pady=(0, 12))
        self.log_text = self._text(wrap, kind='log', height=10, wrap='none',
                                   font=(UI_FONT, 9))
        self.log_text.configure(state='disabled', padx=12, pady=10)
        vsb = ttk.Scrollbar(wrap, orient='vertical', command=self.log_text.yview)
        hsb = ttk.Scrollbar(wrap, orient='horizontal', command=self.log_text.xview)
        self.log_text.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side='right', fill='y')
        hsb.pack(side='bottom', fill='x')
        self.log_text.pack(side='left', fill='both', expand=True)
        for tag, key in (('ok', 'ok'), ('err', 'err'), ('warn', 'warn'),
                         ('dim', 'muted'), ('head', 'accent')):
            self.log_text.tag_configure(tag, foreground=self.t[key])

    def _build_footer(self):
        bar = self._frame(self, 'card')
        bar.pack(fill='x', side='bottom')
        self.var_footer = tk.StringVar(value='准备就绪')
        self._label(bar, textvariable=self.var_footer, fg='muted', bg='card',
                    font=(UI_FONT, 9), anchor='w').pack(side='left', padx=16, pady=7)
        self.var_footer_right = tk.StringVar(value='')
        self._label(bar, textvariable=self.var_footer_right, fg='muted', bg='card',
                    font=(UI_FONT, 9), anchor='e').pack(side='right', padx=16, pady=7)

    # ---------- 配置 ----------
    def load_config(self):
        cfg = json.loads(json.dumps(CONFIG_DEFAULTS))
        disk = read_json(CONFIG_FILE, {})
        if isinstance(disk, dict):
            for key, value in disk.items():
                if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                    cfg[key].update(value)
                else:
                    cfg[key] = value
        if not isinstance(cfg.get('accounts'), list):
            cfg['accounts'] = []
        accounts = []
        for item in cfg['accounts']:
            if not isinstance(item, dict):
                continue
            if not (item.get('refreshToken') or item.get('accessToken')):
                continue
            acc = dict(item)
            acc['enabled'] = bool(acc.get('enabled', True))
            accounts.append(acc)
        cfg['accounts'] = accounts
        if not isinstance(cfg.get('ui'), dict):
            cfg['ui'] = {'theme': 'light'}
        return cfg

    def collect_config(self, strict=False):
        """把界面控件收集成配置字典；strict=True 时格式错误抛 ValueError。"""
        def int_in(var, lo, hi, default, label):
            try:
                value = int(str(var.get()).strip())
            except Exception:
                value = None
            if value is None or not (lo <= value <= hi):
                if strict:
                    raise ValueError('%s 需要填 %d~%d 之间的整数' % (label, lo, hi))
                return default
            return value

        time_text = str(self.var_time.get()).strip()
        if parse_hhmm(time_text) is None:
            if strict:
                raise ValueError('签到时间格式应为 HH:MM，例如 08:00')
            time_text = CONFIG_DEFAULTS['schedule']['time']
        deadline_text = str(self.var_deadline.get()).strip()
        if parse_hhmm(deadline_text) is None:
            if strict:
                raise ValueError('补签截止时间格式应为 HH:MM，例如 23:30')
            deadline_text = CONFIG_DEFAULTS['schedule']['retryDeadline']

        return {
            'version': 1,
            'accounts': [dict(a) for a in self.cfg.get('accounts', [])],
            'device': {
                'keyPem': text_get(self.txt_dev_key),
                'pubPem': text_get(self.txt_dev_pub),
                'machineId': self.var_dev_mid.get().strip(),
                'deviceId': self.var_dev_did.get().strip(),
                'name': self.var_dev_name.get().strip(),
                'model': self.var_dev_model.get().strip(),
                'brand': self.var_dev_brand.get().strip(),
            },
            'push': {
                'qywxToken': self.var_qywx.get().strip(),
                'plusplusToken': self.var_plusplus.get().strip(),
            },
            'schedule': {
                'time': time_text,
                'retryEnabled': bool(self.var_retry.get()),
                'retryIntervalMin': int_in(self.var_retry_min, 5, 720, 60, '补签间隔'),
                'retryMax': int_in(self.var_retry_max, 1, 30, 6, '补签次数'),
                'retryDeadline': deadline_text,
                'catchUp': bool(self.var_catchup.get()),
            },
            'ui': {'theme': self.theme_name},
            'lastAutoDate': str(self.cfg.get('lastAutoDate') or ''),
        }

    def save_config(self, silent=False, strict=False):
        try:
            cfg = self.collect_config(strict=strict)
        except ValueError as e:
            if not silent:
                messagebox.showwarning('配置有误', str(e), parent=self)
            return False
        try:
            write_json_atomic(CONFIG_FILE, cfg)
        except Exception as e:
            self.log_ui('配置保存失败：%s' % e, 'err')
            if not silent:
                messagebox.showerror('保存失败', str(e), parent=self)
            return False
        self.cfg = cfg
        if not silent:
            self.log_ui('配置已保存', 'ok')
            self.var_footer.set('配置已保存 · %s' % bj_now().strftime('%H:%M:%S'))
        self._update_chips()
        return True

    def build_trae_config(self):
        """生成交给 trae_checkin.py 的 TRAE_CONFIG（只含非空字段）。"""
        import contextlib
        import io
        try:
            import trae_checkin as checkin
        except Exception:                                    # noqa: BLE001
            checkin = None
        payload = {}
        accounts = []
        for acc in self.cfg.get('accounts', []):
            if not acc.get('enabled', True):
                continue
            item = {}
            for key in ('refreshToken', 'accessToken', 'uid', 'name', 'deviceId',
                        'realDeviceId', 'checkinDeviceId', 'deviceKeyPem', 'devicePubPem',
                        'machineId'):
                value = str(acc.get(key) or '').strip()
                if value:
                    item[key] = value
            # 签到设备号现算一遍：必须是客户端真实注册号，配置里留着旧值会让签到被判 9074。
            if checkin is not None:
                try:
                    with contextlib.redirect_stdout(io.StringIO()):
                        owned = checkin.account_real_device_ids(acc)
                    if owned:
                        item['checkinDeviceId'] = owned[0]
                except Exception:                            # noqa: BLE001
                    pass
            if item.get('refreshToken') or item.get('accessToken'):
                accounts.append(item)
        if accounts:
            payload['accounts'] = accounts
        device = {k: v for k, v in self.collect_config(False)['device'].items() if v}
        if device:
            payload['device'] = device
        push = {k: v for k, v in self.collect_config(False)['push'].items() if v}
        if push:
            payload['push'] = push
        return payload

    # ---------- 状态展示 ----------
    def enabled_accounts(self):
        return [a for a in self.cfg.get('accounts', []) if a.get('enabled', True)]

    def _needs_device_sync(self):
        for acc in self.enabled_accounts():
            if not str(acc.get('realDeviceId') or acc.get('deviceId') or '').strip():
                return True
        return False

    def count_done(self):
        state = read_state()
        done = state.get('done') or {}
        accounts = self.enabled_accounts()
        hit = sum(1 for a in accounts if acct_key(a) in done)
        return hit, len(accounts)

    def refresh_status(self):
        state = read_state()
        done = state.get('done') or {}
        cooldowns = state.get('cooldown') or {}
        items = self.tree.get_children()
        if items:
            self.tree.delete(*items)
        for idx, acc in enumerate(self.cfg.get('accounts', [])):
            key = acct_key(acc)
            rec = done.get(key)
            left = cooldown_left(cooldowns.get(key))
            tag = 'pending'
            live = (acc.get('credits')
                    if str(acc.get('creditsDate') or '') == today_str() else None)
            if not acc.get('enabled', True):
                state_text, credits, tag = '已停用', '-', 'off'
            elif rec:
                state_text = '已签到'
                credits = self._fmt_credits(live if live is not None else rec.get('credits'))
                tag = 'done'
            elif left > 0:
                state_text = '冷却中 %d 分' % ((left + 59) // 60)
                credits = self._fmt_credits(live) if live is not None else '-'
                tag = 'fail'
            else:
                state_text = '待签到'
                credits = self._fmt_credits(live) if live is not None else '-'
            if self._credits_pending and acc.get('enabled', True):
                credits = '…'      # 正在拉新余额：先别显示旧缓存，避免数字闪一下再变
            ref = str(acc.get('refreshToken') or '')
            shown = ref if self.var_show_tokens.get() else mask_token(ref)
            name = str(acc.get('name') or '账号%d' % (idx + 1))
            if not acc.get('enabled', True):
                name = '[停用] ' + name
            if not ref and acc.get('accessToken'):
                name = '[access] ' + name
                shown = mask_token(acc.get('accessToken'))
            row_tag = 'row_even' if idx % 2 == 0 else 'row_odd'
            bind = str(acc.get('client') or '自动')
            if not (acc.get('deviceKeyPem') and acc.get('devicePubPem')):
                bind += '（缺密钥）'
            self.tree.insert('', 'end', iid=str(idx), tags=(row_tag, tag), values=(
                state_text, name, str(acc.get('uid') or '-'), shown or '-',
                str(acc.get('checkinDeviceId') or acc.get('realDeviceId')
                    or acc.get('deviceId') or '-'), bind, credits))
        total = len(self.cfg.get('accounts', []))
        enabled = len(self.enabled_accounts())
        self.var_account_count.set('共 %d 个账号（启用 %d 个）' % (total, enabled))
        if total:
            self.empty_hint.place_forget()
        else:
            self.empty_hint.place(relx=0.5, rely=0.5, anchor='center')
        self._update_chips()

    @staticmethod
    def _fmt_credits(value):
        try:
            return '{:,.2f}'.format(float(value))
        except Exception:
            return str(value or '-')

    def _update_chips(self):
        done, total = self.count_done()
        if total == 0:
            self.var_chip_status.set('尚未配置账号')
        else:
            self.var_chip_status.set('今日 %d/%d 已签到' % (done, total))
        hhmm = parse_hhmm(str(self.var_time.get()), (8, 0))
        now = bj_now()
        target = now.replace(hour=hhmm[0], minute=hhmm[1], second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
            prefix = '明天'
        else:
            prefix = '今天'
        text = '下次自动：%s %02d:%02d' % (prefix, target.hour, target.minute)
        if self.retry_at:
            text += ' · 补签 %s' % self.retry_at.strftime('%H:%M')
        self.var_chip_next.set(text)
        missing = sum(1 for a in self.enabled_accounts()
                      if not str(a.get('realDeviceId') or a.get('deviceId') or '').strip())
        self.var_footer_right.set(
            '注意：%d 个账号缺少设备号，请点「读取本机账号」同步' % missing
            if missing else '')

    # ---------- 账号操作 ----------
    def _next_default_name(self):
        used = {str(a.get('name') or '') for a in self.cfg.get('accounts', [])}
        i = len(self.cfg.get('accounts', [])) + 1
        while ('账号%d' % i) in used:
            i += 1
        return '账号%d' % i

    def _after_accounts_changed(self, log_text=None):
        self.save_config(silent=True)
        self.refresh_status()
        if log_text:
            self.log_ui(log_text, 'ok')
        done, total = self.count_done()
        if total and done < total and self.cfg.get('lastAutoDate') == today_str():
            self.var_footer.set('有账号今天还没签到，可点「立即签到」补上')
        self._update_chips()

    # ---------- 切换客户端账号（冷切换） ----------
    def _switch_by_name(self, name):
        for idx, acc in enumerate(self.cfg.get('accounts', [])):
            if str(acc.get('name') or '') == name or str(acc.get('uid') or '') == name:
                try:
                    self.tree.selection_set(str(idx))
                except Exception:
                    pass
                return self.switch_to_selected()
        messagebox.showwarning('找不到账号', '账号列表里没有「%s」。' % name, parent=self)

    def switch_to_selected(self):
        if SWITCH is None:
            messagebox.showerror('缺少模块', '同目录下找不到 trae_switch.py / trae_km.py。',
                                 parent=self)
            return
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo('切换账号', '请先在列表里选中要切换到的账号。', parent=self)
            return
        if self.switching:
            messagebox.showinfo('正在切换', '上一次切换还没结束，请稍候…', parent=self)
            return
        if self.running or self.web_running:
            messagebox.showwarning('有任务在进行', '请等签到 / 批量登录结束后再切换账号。',
                                   parent=self)
            return
        acc = self.cfg['accounts'][int(sel[0])]
        name = str(acc.get('name') or acc.get('uid') or '目标账号')
        if not acc.get('refreshToken'):
            messagebox.showwarning('缺少凭据', '该账号没有 refreshToken，请先重新登录。',
                                   parent=self)
            return
        if not messagebox.askyesno(
                '切换客户端账号',
                '将把 Trae 客户端切换到「%s」：\n\n'
                '1) 自动结束正在运行的 Trae 进程\n'
                '2) 备份当前登录态后写入目标账号（storage.json）\n'
                '3) 重新拉起客户端，并核对是否真的落到了目标账号\n\n'
                '未保存的编辑器内容会丢失，请先保存。继续吗？' % name,
                parent=self):
            return
        self.switching = True
        self.btn_switch.configure(state='disabled', text='切换中…')
        self.log_ui('', 'dim')
        self.log_ui('▶ 开始切换客户端到「%s」' % name, 'head')
        threading.Thread(target=self._switch_worker, args=(dict(acc), name),
                         daemon=True).start()

    def _switch_worker(self, acc, name):
        def log(text, level='dim'):
            self.msgq.put(('log', text))

        live, pair = {}, {}
        try:
            res = SWITCH.switch_to(acc, log=log)
        except Exception as exc:                           # noqa: BLE001
            res = {'ok': False, 'message': '切换异常：%s' % exc, 'outcome': 'error'}
            log('切换异常：%s' % exc, 'err')
        try:
            live = SWITCH.describe_live()
            pair = SWITCH.device_pair(device_id=str(acc.get('realDeviceId')
                                                    or acc.get('deviceId') or ''))
        except Exception:
            pass
        self.msgq.put(('switch_done', {'name': name, 'result': res,
                                       'live': live, 'pair': pair}))

    def _on_switch_done(self, payload):
        self.switching = False
        self.btn_switch.configure(state='normal', text='切换到此账号')
        res = payload.get('result') or {}
        name = payload.get('name') or '账号'
        pair = payload.get('pair') or {}
        if res.get('ok'):
            for item in self.cfg.get('accounts', []):
                if str(item.get('name') or '') == name:
                    if pair.get('privateKeyPem'):
                        item['deviceKeyPem'] = pair['privateKeyPem']
                    if pair.get('publicKeyPem'):
                        item['devicePubPem'] = pair['publicKeyPem']
                    if pair.get('machineId') and not item.get('machineId'):
                        item['machineId'] = pair['machineId']
            self._after_accounts_changed()
            self.log_ui('✅ 切换完成：%s' % (res.get('message') or ''), 'ok')
            messagebox.showinfo('切换完成', res.get('message') or '已切换', parent=self)
        else:
            self.log_ui('❌ 切换失败：%s' % (res.get('message') or '未知原因'), 'err')
            messagebox.showwarning('切换失败', res.get('message') or '未知原因', parent=self)
        self.refresh_live_account()

    def _auto_bind_accounts(self):
        """启动时给缺绑定/缺设备密钥的账号补全（只补空缺，不覆盖已有值）。"""
        changed = 0
        try:
            import contextlib
            import io
            import trae_checkin as checkin
        except Exception:                                   # noqa: BLE001
            checkin = None
        for acc in self.cfg.get('accounts', []):
            # 签到设备号：必须是客户端真实注册号。网页登录自造的号能续期但会被判 9074，
            # 所以这里把「本机登录着该号的客户端注册号」单独记进 checkinDeviceId。
            if checkin is not None:
                try:
                    with contextlib.redirect_stdout(io.StringIO()):
                        owned = checkin.account_real_device_ids(acc)
                    if owned and owned[0] != str(acc.get('checkinDeviceId') or ''):
                        acc['checkinDeviceId'] = owned[0]
                        changed += 1
                except Exception:                        # noqa: BLE001
                    pass
            if SWITCH is None:
                continue
            if acc.get('deviceKeyPem') and acc.get('devicePubPem') and acc.get('client'):
                continue
            info = SWITCH.auto_bind(acc)
            if not info:
                continue
            for key in ('client', 'deviceKeyPem', 'devicePubPem', 'machineId', 'realDeviceId'):
                if info.get(key) and not acc.get(key):
                    acc[key] = info[key]
            if info.get('realDeviceId') and not (acc.get('deviceKeyPem') and acc.get('devicePubPem')):
                # 只有账号自己没有设备密钥（即没绑定任何签发身份）时，才把续期身份指向客户端；
                # 网页登录自造过身份的账号必须保留它自己的密钥，否则续期会 10101。
                acc['deviceId'] = info['realDeviceId']
            changed += 1
        if changed:
            self.save_config(silent=True)
            self.refresh_status()
            self.log_ui('已为 %d 个账号补全客户端绑定与设备密钥' % changed, 'dim')

    def refresh_live_account(self):
        """在账号列表底部显示客户端当前登录的是哪个号。"""
        if SWITCH is None:
            return
        try:
            info = SWITCH.describe_live()
        except Exception:
            return
        if info.get('loggedIn'):
            self.var_live_account.set('客户端当前：%s（UID %s）'
                                      % (info.get('username') or '-', info.get('uid') or '-'))
        else:
            self.var_live_account.set('客户端当前：未登录')

    def _build_row_menu(self):
        t = self.t
        self.row_menu = tk.Menu(self, tearoff=0, bg=t['card'], fg=t['text'],
                                activebackground=t['accent'], activeforeground=t['accent_fg'],
                                bd=0, relief='flat', activeborderwidth=0)
        for label, action in (('重命名备注', self.rename_account),
                              ('编辑凭据…', self.edit_account),
                              ('切换到此账号', self.switch_to_selected),
                              ('启用 / 停用', self.toggle_account),
                              ('复制 UID', self.copy_uid),
                              ('刷新列表', self.refresh_list),
                              (None, None),
                              ('删除账号', self.delete_account)):
            if label is None:
                self.row_menu.add_separator()
            else:
                self.row_menu.add_command(label=label, command=action)

    def _show_row_menu(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return 'break'
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        try:
            self.row_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.row_menu.grab_release()
        return 'break'

    def _selected_account(self, action='操作'):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo(action, '请先在列表里选中一个账号。', parent=self)
            return None, None
        idx = int(sel[0])
        return idx, self.cfg['accounts'][idx]

    def rename_account(self):
        idx, acc = self._selected_account('重命名')
        if acc is None:
            return
        old = str(acc.get('name') or acc.get('uid') or '账号%d' % (idx + 1))
        new = simpledialog.askstring('重命名备注', '新的备注名（只影响面板显示）：',
                                     initialvalue=old, parent=self)
        if new is None:
            return
        new = new.strip()
        if not new or new == old:
            return
        acc['name'] = new
        self._after_accounts_changed('已重命名为：%s' % new)

    def copy_uid(self):
        idx, acc = self._selected_account('复制 UID')
        if acc is None:
            return
        uid = str(acc.get('uid') or '').strip()
        if not uid:
            messagebox.showinfo('复制 UID', '该账号没有 UID，请先同步一次登录态。', parent=self)
            return
        self.clipboard_clear()
        self.clipboard_append(uid)
        self.var_footer.set('已复制 UID：%s' % uid)

    def add_account(self):
        choice = AddAccountDialog(self).result
        if choice == 'manual':
            dlg = AccountDialog(self, None, self._next_default_name())
            if dlg.result:
                self.cfg.setdefault('accounts', []).append(dlg.result)
                self._after_accounts_changed('已添加账号：%s' % dlg.result.get('name'))
        elif choice == 'web':
            self.login_from_browser()

    def login_from_browser(self):
        """打开官网授权页登录一个号，成功后自动入库。"""
        if WEB is None:
            messagebox.showerror('缺少模块', '同目录下找不到 trae_weblogin.py，无法浏览器登录。',
                                 parent=self)
            return
        if self.web_running:
            messagebox.showinfo('正在进行', '上一次浏览器登录还没结束，请稍候…', parent=self)
            return
        name = simpledialog.askstring(
            '从浏览器获取', '给这个账号起个备注名（留空则自动用登录账号的昵称）：',
            initialvalue='', parent=self)
        if name is None:                       # 点了取消
            return
        name = name.strip()
        self.web_running = True
        self.web_stop.clear()
        self._web_url = ''
        self.web_dialog = WebAuthDialog(self, on_cancel=self.cancel_web_login,
                                        on_reopen=self.reopen_auth_url)
        self.var_chip_status.set('等待浏览器授权…')
        self.log_ui('', 'dim')
        self.log_ui('▶ 打开浏览器登录%s，用手机号 + 验证码完成授权'
                    % ('「%s」' % name if name else ''), 'head')
        threading.Thread(target=self._web_login_worker, args=(name, 300),
                         daemon=True).start()

    def reopen_auth_url(self):
        if not self._web_url:
            return
        import webbrowser
        webbrowser.open(self._web_url)

    def cancel_web_login(self):
        if not self.web_running:
            if self.web_dialog:
                self.web_dialog.close()
                self.web_dialog = None
            return
        self.web_stop.set()
        if self.web_dialog:
            self.web_dialog.set_status('正在取消授权…', 'warn')
        self.log_ui('已取消本次授权', 'warn')

    def _web_login_worker(self, name, timeout):
        def log(text, level='dim'):
            self.msgq.put(('log', text))

        def on_url(url):
            self.msgq.put(('web_url', url))

        try:
            res = WEB.web_login(name=name, timeout=timeout, log=log,
                                stop=self.web_stop, on_url=on_url)
        except Exception as exc:                            # noqa: BLE001
            res = {'ok': False, 'error': '登录异常：%s' % exc, 'account': None}
        self.msgq.put(('web_login_done', {'name': name, 'result': res}))

    def _on_web_login_done(self, payload):
        self.web_running = False
        if self.web_dialog:
            self.web_dialog.close()
            self.web_dialog = None
        res = payload.get('result') or {}
        name = str(payload.get('name') or '')
        if res.get('ok') and res.get('account'):
            acc = res['account']
            self._merge_account(acc)
            shown = acc.get('name') or acc.get('username') or name or acc.get('uid')
            self.var_footer.set('已添加账号 · %s' % shown)
            self.log_ui('✅ 授权完成，已添加账号：%s' % shown, 'ok')
            return
        err = res.get('error') or '未知原因'
        if err == '已取消':
            self.var_footer.set('已取消浏览器登录')
            return
        self.log_ui('❌ 浏览器登录失败：%s' % err, 'err')
        self.var_footer.set('浏览器登录失败')
        messagebox.showwarning('登录失败', err, parent=self)

    def _merge_account(self, account):
        """把新登录/导入的账号并入配置：同 uid 或同 token 视为同一个号，只更新凭据。"""
        acc = dict(account or {})
        if not acc.get('refreshToken'):
            return False
        if not str(acc.get('name') or '').strip():
            acc['name'] = (str(acc.get('username') or '').strip()
                           or str(acc.get('uid') or '').strip() or '未命名账号')
        accounts = self.cfg.setdefault('accounts', [])
        uid = str(acc.get('uid') or '')
        token_key = str(acc.get('refreshToken') or '')[:24]
        target = None
        for item in accounts:
            if uid and str(item.get('uid') or '') == uid:
                target = item
                break
            if token_key and str(item.get('refreshToken') or '')[:24] == token_key:
                target = item
                break
        if target is None:
            accounts.append(acc)
            text = '已新增账号：%s' % (acc.get('name') or acc.get('uid'))
        else:
            for key in ('refreshToken', 'accessToken', 'deviceId', 'realDeviceId',
                        'deviceKeyPem', 'devicePubPem', 'machineId', 'oauthDevice',
                        'source', 'username'):
                if acc.get(key):
                    target[key] = acc[key]
            if not str(target.get('name') or '').strip():
                target['name'] = acc.get('name')
            text = '已更新账号：%s' % (target.get('name') or target.get('uid'))
        self._after_accounts_changed(text)
        return True

    def edit_account(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo('编辑账号', '请先在列表里选中一个账号。', parent=self)
            return
        idx = int(sel[0])
        dlg = AccountDialog(self, self.cfg['accounts'][idx], self._next_default_name())
        if not dlg.result:
            return
        self.cfg['accounts'][idx] = dlg.result
        self._after_accounts_changed('已更新账号：%s' % dlg.result.get('name'))

    def delete_account(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo('删除账号', '请先在列表里选中一个账号。', parent=self)
            return
        idx = int(sel[0])
        acc = self.cfg['accounts'][idx]
        if not messagebox.askyesno(
                '删除账号', '确定删除「%s」吗？' % (acc.get('name') or '账号%d' % (idx + 1)),
                parent=self):
            return
        self.cfg['accounts'].pop(idx)
        self._after_accounts_changed('已删除账号')

    def toggle_account(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo('启用/停用', '请先在列表里选中一个账号。', parent=self)
            return
        idx = int(sel[0])
        acc = self.cfg['accounts'][idx]
        acc['enabled'] = not acc.get('enabled', True)
        self._after_accounts_changed('%s已%s' % (
            acc.get('name') or '账号%d' % (idx + 1), '启用' if acc['enabled'] else '停用'))

    # ---------- 本机凭据读取 ----------
    def extract_accounts(self, auto=False):
        if self.extracting:
            return
        if not os.path.isfile(TOKEN_SCRIPT):
            self.log_ui('找不到 trae_get_token.py，无法读取本机账号', 'err')
            if not auto:
                messagebox.showerror('缺少脚本', '同目录下找不到 trae_get_token.py。', parent=self)
            return
        self.extracting = True
        self.log_ui('正在读取本机 Trae 客户端登录态…', 'head')

        def work():
            try:
                p = subprocess.run([sys.executable, TOKEN_SCRIPT, '--accounts'],
                                   cwd=HERE, capture_output=True, timeout=120,
                                   creationflags=CREATE_NO_WINDOW)
                out = p.stdout.decode('utf-8', 'replace')
                err = p.stderr.decode('utf-8', 'replace')
                accounts = self.parse_accounts_output(out)
                self.msgq.put(('extract_accounts', {
                    'auto': auto, 'ok': bool(accounts), 'accounts': accounts,
                    'code': p.returncode, 'err': err.strip()}))
            except Exception as e:
                self.msgq.put(('extract_accounts', {
                    'auto': auto, 'ok': False, 'accounts': [], 'code': -1, 'err': str(e)}))

        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def parse_accounts_output(out):
        for line in reversed(str(out or '').splitlines()):
            text = line.strip()
            if text.startswith('[') and text.endswith(']'):
                try:
                    data = json.loads(text)
                except Exception:
                    continue
                if isinstance(data, list):
                    return [a for a in data if isinstance(a, dict)
                            and (a.get('refreshToken') or a.get('accessToken'))]
        return []

    def _on_extract_accounts(self, result):
        try:
            self._apply_extract_accounts(result)
        finally:
            self._resume_pending_run()

    def _resume_pending_run(self):
        pending = self._pending_run
        if not pending or self.running:
            return
        self._pending_run = None
        reason, interactive = pending
        self.after(150, lambda: self.start_run(reason, interactive, sync=False))

    def _apply_extract_accounts(self, result):
        self.extracting = False
        if not result.get('ok'):
            msg = (result.get('err') or '').strip().splitlines()
            msg = msg[0][:160] if msg else '没有读到可用账号'
            self.log_ui('读取本机账号失败：%s' % msg, 'err')
            self.log_ui('请先打开 Trae 客户端并登录，再点「读取本机账号」。', 'dim')
            if not result.get('auto'):
                messagebox.showwarning('读取失败', msg + '\n\n请先在 Trae 客户端登录后再试。',
                                       parent=self)
            return
        accounts = result['accounts']
        self.log_ui('读取到 %d 个账号' % len(accounts), 'ok')
        if not self.cfg.get('accounts'):
            mode = 'replace'
        elif result.get('auto'):
            mode = 'append'
        else:
            answer = messagebox.askyesnocancel(
                '读取到 %d 个账号' % len(accounts),
                '要把这些账号追加到现有列表吗？\n\n'
                '「是」= 追加（相同的自动去重）\n'
                '「否」= 替换整个账号列表\n'
                '「取消」= 不导入', parent=self)
            if answer is None:
                self.log_ui('已取消导入', 'dim')
                return
            mode = 'append' if answer else 'replace'

        if mode == 'replace':
            new_accounts = []
        else:
            new_accounts = [dict(a) for a in self.cfg.get('accounts', [])]
        by_key = {acct_key(a): a for a in new_accounts}
        by_token = {str(a.get('refreshToken') or '')[:24]: a
                    for a in new_accounts if a.get('refreshToken')}
        added = updated = 0
        for raw in accounts:
            acc = dict(raw)
            acc['enabled'] = True
            if not acc.get('uid') and acc.get('accessToken'):
                acc['uid'] = jwt_uid(acc['accessToken'])
            if not acc.get('name'):
                acc['name'] = acc.get('uid') or ('账号%d' % (len(new_accounts) + 1))
            key = acct_key(acc)
            token_key = str(acc.get('refreshToken') or '')[:24]
            target = by_key.get(key) or (by_token.get(token_key) if token_key else None)
            if target is not None:
                # 已存在：补全/纠正设备号与更新的凭据
                did = str(acc.get('realDeviceId') or acc.get('deviceId') or '').strip()
                old_did = str(target.get('realDeviceId') or target.get('deviceId') or '').strip()
                changed = False
                if did and did != old_did:
                    if target.get('deviceKeyPem') and target.get('devicePubPem'):
                        # 账号已绑定自己的签发身份（如网页登录自造密钥），token 就是那套签的：
                        # 改 realDeviceId 会让续期 10101，所以客户端真号只记成「签到设备号」。
                        if str(target.get('checkinDeviceId') or '') != did:
                            target['checkinDeviceId'] = did
                            changed = True
                    else:
                        target['realDeviceId'] = did
                        changed = True
                new_at = str(acc.get('accessToken') or '').strip()
                old_at = str(target.get('accessToken') or '').strip()
                if new_at and new_at != old_at and (
                        not old_at or jwt_exp(new_at) > jwt_exp(old_at)):
                    target['accessToken'] = new_at
                    if acc.get('refreshToken'):
                        target['refreshToken'] = acc['refreshToken']
                    changed = True
                for field in ('uid', 'name'):
                    if not str(target.get(field) or '').strip() and acc.get(field):
                        target[field] = acc[field]
                        changed = True
                if changed:
                    updated += 1
                continue
            by_key[key] = acc
            if token_key:
                by_token[token_key] = acc
            new_accounts.append(acc)
            added += 1

        if mode == 'append' and added == 0 and updated == 0:
            self.log_ui('没有新账号可导入（都已存在，设备号也一致）', 'dim')
            return
        self.cfg['accounts'] = new_accounts
        if mode == 'append':
            text = '账号同步完成：新增 %d 个，更新设备信息 %d 个，当前共 %d 个' % (
                added, updated, len(new_accounts))
        else:
            text = '账号导入完成：替换为 %d 个账号' % len(new_accounts)
        self._after_accounts_changed(text)
        if mode == 'replace':
            self._auto_fill_device_hint(accounts)

    def _auto_fill_device_hint(self, accounts):
        if any(a.get('realDeviceId') for a in accounts):
            self.log_ui('已随账号带上客户端真实设备号', 'dim')
        else:
            self.log_ui('提示：账号里没有设备号，签到可能被拒成 9074；'
                        '请确认 Trae 客户端已登录后再读取一次。', 'warn')

    def extract_keys(self):
        if self.extracting:
            return
        if not os.path.isfile(TOKEN_SCRIPT):
            messagebox.showerror('缺少脚本', '同目录下找不到 trae_get_token.py。', parent=self)
            return
        self.extracting = True
        self.log_ui('正在读取本机设备密钥…', 'head')

        def work():
            try:
                p = subprocess.run([sys.executable, TOKEN_SCRIPT, '--export-keys'],
                                   cwd=HERE, capture_output=True, timeout=120,
                                   creationflags=CREATE_NO_WINDOW)
                out = p.stdout.decode('utf-8', 'replace')
                err = p.stderr.decode('utf-8', 'replace')
                self.msgq.put(('extract_keys', {
                    'ok': p.returncode == 0, 'out': out, 'err': err.strip()}))
            except Exception as e:
                self.msgq.put(('extract_keys', {'ok': False, 'out': '', 'err': str(e)}))

        threading.Thread(target=work, daemon=True).start()

    def _on_extract_keys(self, result):
        self.extracting = False
        if not result.get('ok'):
            msg = (result.get('err') or result.get('out') or '读取失败').strip().splitlines()
            msg = msg[0][:160] if msg else '读取失败'
            self.log_ui('读取设备密钥失败：%s' % msg, 'err')
            messagebox.showwarning('读取失败', msg + '\n\n请先在 Trae 客户端登录后再试。',
                                   parent=self)
            return
        values = {}
        for line in str(result.get('out') or '').splitlines():
            if '=' not in line:
                continue
            name, _, value = line.partition('=')
            name = name.strip()
            if name in ('TRAE_DEVICE_ID', 'TRAE_MACHINE_ID',
                        'TRAE_DEVICE_KEY_PEM', 'TRAE_DEVICE_PUB_PEM'):
                values[name] = value.strip().replace('\\n', '\n')
        if not values:
            self.log_ui('没有解析到设备字段，请确认 Trae 客户端已登录', 'warn')
            return
        text_set(self.txt_dev_key, values.get('TRAE_DEVICE_KEY_PEM', ''))
        text_set(self.txt_dev_pub, values.get('TRAE_DEVICE_PUB_PEM', ''))
        self.var_dev_did.set(values.get('TRAE_DEVICE_ID', ''))
        self.var_dev_mid.set(values.get('TRAE_MACHINE_ID', ''))
        self.save_config(silent=True)
        self.log_ui('设备密钥已读取并保存（%d 项）' % len(values), 'ok')

    # ---------- 导入 / 导出 ----------
    def export_config(self):
        if not self.save_config(silent=True):
            return
        path = filedialog.asksaveasfilename(
            parent=self, title='导出配置', defaultextension='.json',
            initialfile='trae_config.json',
            filetypes=[('JSON 文件', '*.json'), ('所有文件', '*.*')])
        if not path:
            return
        try:
            write_json_atomic(path, self.cfg)
            self.log_ui('配置已导出：%s' % path, 'ok')
        except Exception as e:
            messagebox.showerror('导出失败', str(e), parent=self)

    def import_config(self):
        path = filedialog.askopenfilename(
            parent=self, title='导入配置',
            filetypes=[('JSON 文件', '*.json'), ('所有文件', '*.*')])
        if not path:
            return
        data = read_json(path, None)
        if data is None:
            messagebox.showerror('导入失败', '文件不是合法 JSON。', parent=self)
            return
        if isinstance(data, list):
            data = {'accounts': data}
        if not isinstance(data, dict):
            messagebox.showerror('导入失败', '配置内容不是对象或数组。', parent=self)
            return
        if not messagebox.askyesno(
                '导入配置',
                '将用文件内容覆盖当前配置（账号、设备、推送、定时、主题）。\n确定导入吗？',
                parent=self):
            return
        accounts = []
        for raw in (data.get('accounts') or []):
            if isinstance(raw, dict) and (raw.get('refreshToken') or raw.get('accessToken')):
                acc = dict(raw)
                acc['enabled'] = bool(acc.get('enabled', True))
                if not acc.get('uid') and acc.get('accessToken'):
                    acc['uid'] = jwt_uid(acc['accessToken'])
                accounts.append(acc)
        merged = {
            'version': 1,
            'accounts': accounts,
            'device': dict(CONFIG_DEFAULTS['device']),
            'push': dict(CONFIG_DEFAULTS['push']),
            'schedule': dict(CONFIG_DEFAULTS['schedule']),
            'ui': dict(CONFIG_DEFAULTS['ui']),
            'lastAutoDate': '',
        }
        for section in ('device', 'push', 'schedule', 'ui'):
            if isinstance(data.get(section), dict):
                merged[section].update(data[section])
        self.cfg = merged
        self.theme_name = str((merged.get('ui') or {}).get('theme') or 'light')
        if self.theme_name not in THEMES:
            self.theme_name = 'light'
        self._apply_config_to_widgets()
        self.apply_theme(self.theme_name, save=False)
        self.save_config(silent=True)
        self.refresh_status()
        self.log_ui('配置已导入：%d 个账号' % len(accounts), 'ok')

    def open_config_dir(self):
        try:
            os.startfile(HERE)   # 仅 Windows 面板
        except Exception as e:
            messagebox.showerror('打开失败', str(e), parent=self)

    def open_log_dir(self):
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            os.startfile(LOG_DIR)
        except Exception as e:
            messagebox.showerror('打开失败', str(e), parent=self)

    # ---------- 开机自启 ----------
    def _toggle_startup(self):
        want = bool(self.var_startup.get())
        ok, msg = set_startup_enabled(want)
        if not ok:
            self.var_startup.set(not want)
            self.log_ui(msg, 'err')
            messagebox.showerror('开机自动启动', msg, parent=self)
        else:
            self.log_ui(msg, 'ok')
            self.var_footer.set(msg)

    # ---------- 运行签到 ----------
    def start_run(self, reason='手动', interactive=True, sync=True):
        if self.running:
            if interactive:
                messagebox.showinfo('正在签到', '上一轮签到还没结束，请稍候…', parent=self)
            return False
        if self.web_running or self.switching:
            if interactive:
                messagebox.showinfo('请稍候', '浏览器授权 / 切换账号还没结束，完成后再签到。',
                                    parent=self)
            return False
        if not os.path.isfile(CHECKIN_SCRIPT):
            self.log_ui('找不到 trae_checkin.py，无法签到', 'err')
            if interactive:
                messagebox.showerror('缺少脚本', '同目录下找不到 trae_checkin.py。', parent=self)
            return False
        try:
            cfg = self.collect_config(strict=True)
        except ValueError as e:
            self.log_ui('配置有误：%s' % e, 'err')
            if interactive:
                messagebox.showwarning('配置有误', str(e), parent=self)
            return False
        accounts = [a for a in cfg['accounts'] if a.get('enabled', True)]
        if not accounts:
            self.log_ui('还没有可用账号，先点「读取本机账号」', 'warn')
            if interactive:
                messagebox.showwarning('没有账号', '请先在「账号列表」读取或添加账号。',
                                       parent=self)
                self.main_tabs.select('accounts')
            return False
        payload = self.build_trae_config()
        if not payload.get('accounts'):
            self.log_ui('账号缺少 refreshToken / accessToken，已跳过本轮', 'err')
            if interactive:
                messagebox.showwarning('账号不完整', '请检查账号凭据是否填写完整。', parent=self)
            return False
        if sync and self._needs_device_sync() and os.path.isfile(TOKEN_SCRIPT):
            # 设备号不全时先同步本机客户端身份，避免拿错设备密钥续期（20403）
            if self._pending_run is None:
                self._pending_run = (reason, interactive)
                self.log_ui('检测到账号缺少设备号，先同步本机设备信息，随后自动开始签到…', 'head')
                if not self.extracting:
                    self.extract_accounts(auto=True)
            return True

        self.running = True
        self._run_interactive = interactive
        self.btn_run.configure(state='disabled', text='签到中…')
        self.var_chip_status.set('正在签到…')
        self.var_footer.set('正在签到，请稍候（多账号会逐个间隔请求）…')
        self.log_ui('', 'dim')
        self.log_ui('▶ %s 开始签到（%s），启用账号 %d 个' % (
            bj_now().strftime('%H:%M:%S'), reason, len(accounts)), 'head')
        threading.Thread(target=self._run_worker, args=(payload, reason),
                         daemon=True).start()
        return True

    def _run_worker(self, payload, reason):
        env = os.environ.copy()
        for key in ('TRAE_CONFIG', 'TRAE_ACCOUNTS', 'TRAE_ONLY'):
            env.pop(key, None)
        env['TRAE_CONFIG'] = json.dumps(payload, ensure_ascii=False)
        env['PYTHONIOENCODING'] = 'utf-8'
        env['PYTHONUNBUFFERED'] = '1'
        try:
            proc = subprocess.Popen(
                [sys.executable, '-u', CHECKIN_SCRIPT],
                cwd=HERE, env=env, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, creationflags=CREATE_NO_WINDOW)
        except Exception as e:
            self.msgq.put(('run_done', {'code': -1, 'reason': reason,
                                        'error': str(e), 'lines': []}))
            return
        lines = []
        for raw in iter(proc.stdout.readline, b''):
            line = raw.decode('utf-8', 'replace').rstrip('\r\n')
            lines.append(line)
            self.msgq.put(('log', line))
        proc.wait()
        self.msgq.put(('run_done', {'code': proc.returncode, 'reason': reason,
                                    'error': '', 'lines': lines}))

    def refresh_list(self):
        """整表刷新：先重画本地状态，再去服务端拉一次实时积分。"""
        if self.running or self.web_running or self.switching:
            self.log_ui('有任务在进行，稍后再刷新', 'warn')
            return
        self.cfg = self.load_config()
        self._credits_pending = True        # 先挂「刷新中」，避免旧积分闪一下
        self._sync_tokens_from_cache()
        self._auto_bind_accounts()
        self.refresh_status()
        self.refresh_live_account()
        self.log_ui('列表已刷新（%d 个账号），正在拉取实时积分…'
                    % len(self.cfg.get('accounts') or []), 'dim')
        self._refresh_credits()

    def _refresh_credits(self):
        """后台拉取每个账号的实时积分，并写回配置（下次启动先用它，再自动刷新）。"""
        accounts = [dict(a) for a in self.cfg.get('accounts', []) if a.get('enabled', True)]
        accounts = [a for a in accounts if a.get('accessToken') or a.get('refreshToken')]
        if not accounts:
            return
        self._credits_pending = True
        self.refresh_status()
        self.var_chip_status.set('正在刷新积分 0/%d…' % len(accounts))
        threading.Thread(target=self._credits_worker, args=(accounts,), daemon=True).start()

    def _credits_worker(self, accounts):
        try:
            import trae_checkin as checkin
            cache = checkin.load_cache()
        except Exception:                                   # noqa: BLE001
            checkin, cache = None, {}
        total = len(accounts)
        for index, acc in enumerate(accounts, 1):
            value = None
            if checkin is not None:
                try:
                    value = checkin.fetch_credits(acc, cache)
                except Exception:                            # noqa: BLE001
                    value = None
            self.msgq.put(('credits', {'index': index, 'total': total,
                                       'uid': str(acc.get('uid') or ''),
                                       'name': acc.get('name'), 'value': value}))

    def _on_credits(self, payload):
        uid = str(payload.get('uid') or '')
        name = payload.get('name')
        value = payload.get('value')
        if value is not None:
            for acc in self.cfg.get('accounts', []):
                matched = ((uid and str(acc.get('uid') or '') == uid)
                           or (not uid and str(acc.get('name') or '') == name))
                if matched:
                    acc['credits'] = round(float(value), 2)
                    acc['creditsDate'] = today_str()
                    break
        index = int(payload.get('index') or 0)
        total = int(payload.get('total') or 0)
        if index >= total:
            self._credits_pending = False
            self.refresh_status()
            self.save_config(silent=True)      # 把最新积分存进配置，下次先用缓存
            self._sync_tokens_from_cache()
            self.var_footer.set('积分已刷新 · %s' % bj_now().strftime('%H:%M:%S'))
            self.log_ui('积分已刷新并写入缓存', 'dim')
        else:
            self.var_chip_status.set('正在刷新积分 %d/%d…' % (index, total))

    def _sync_tokens_from_cache(self):
        """把签到进程续期后的新 token 合并回面板配置。

        续期会轮换 refreshToken：配置里留着旧的那条，「切换客户端账号」就会把
        已失效的 token 写进客户端，客户端随后会掉登录。
        """
        accounts = self.cfg.get('accounts') or []
        if not accounts:
            return
        try:
            import contextlib
            import io
            import trae_checkin as checkin
            before = {str(a.get('uid') or a.get('name')): a.get('refreshToken')
                      for a in accounts}
            # 它内部会 print 一行缓存提示；面板里没有控制台，顺手吞掉（避免 pythonw 下没有 stdout）
            with contextlib.redirect_stdout(io.StringIO()):
                checkin.apply_cache(accounts)
        except Exception as exc:                            # noqa: BLE001
            self.log_ui('合并续期 token 失败：%s' % exc, 'dim')
            return
        changed = [a for a in accounts
                   if before.get(str(a.get('uid') or a.get('name')), None)
                   not in (None, a.get('refreshToken'))]
        if changed:
            self._after_accounts_changed('已用续期后的新 token 更新 %d 个账号的配置'
                                         % len(changed))

    def _on_run_done(self, result):
        self.running = False
        self.btn_run.configure(state='normal', text='立即签到')
        self._sync_tokens_from_cache()
        self._refresh_credits()
        if result.get('error'):
            self.log_ui('启动签到进程失败：%s' % result['error'], 'err')
            self.var_footer.set('签到进程启动失败')
            if self._run_interactive:
                messagebox.showerror('签到失败', result['error'], parent=self)
            return
        lines = result.get('lines') or []
        parsed = {}
        for line in lines:
            m = RESULT_RE.match(line)
            if m:
                parsed[m.group(2).strip()] = m.group(1)
        bad = [name for name, status in parsed.items() if status in BAD_STATUS]
        done, total = self.count_done()
        self.refresh_status()

        if total == 0:
            summary = '没有可签到账号'
            level = 'warn'
        elif done >= total:
            summary = '全部完成：%d/%d 个账号已签到' % (done, total)
            level = 'ok'
        else:
            summary = '已完成 %d/%d，还有 %d 个未签' % (done, total, total - done)
            level = 'warn'
        if bad:
            summary += '（%s）' % '、'.join(bad[:3])
        self.log_ui('⏹ 本轮结束：%s（退出码 %s）' % (summary, result.get('code')), level)
        self.var_footer.set(summary)
        if result.get('code') not in (0, None) and done < total:
            self.log_ui('提示：最常见原因是 refreshToken 过期或设备号不对，'
                        '可点「读取本机账号」重新提取。', 'dim')
        if any(('20403' in line) or ('Token device not match' in line) for line in lines):
            self.log_ui('提示：该账号的 token 与设备号不匹配。面板已按客户端设备号自动匹配密钥；'
                        '若仍失败，请确认对应客户端（如 TRAE SOLO CN）仍保持登录后，'
                        '点「读取本机账号」同步设备号再签到。', 'warn')
        self._plan_retry()
        self._update_chips()
        if self._run_interactive:
            messagebox.showinfo('签到完成', summary + '\n\n详细过程见「运行日志」。', parent=self)

    # ---------- 自动调度 ----------
    def _plan_retry(self):
        self.retry_at = None
        if self.all_done():
            self.log_ui('🎉 今日所有启用账号都已签到', 'ok')
            self.var_footer.set('今日所有账号已签到完成')
            return
        try:
            cfg = self.collect_config(strict=False)
        except Exception:
            return
        sch = cfg['schedule']
        if not sch['retryEnabled']:
            return
        now = bj_now()
        hhmm = parse_hhmm(sch['retryDeadline'], (23, 30))
        deadline = now.replace(hour=hhmm[0], minute=hhmm[1], second=0, microsecond=0)
        if now >= deadline:
            self.log_ui('已过补签截止时间，今日不再自动重试', 'warn')
            return
        if self.retry_count >= int(sch['retryMax']):
            self.log_ui('已达到最大补签次数（%s 次），今日停止自动重试' % sch['retryMax'], 'warn')
            return
        self.retry_at = now + timedelta(minutes=int(sch['retryIntervalMin']))
        self.retry_count += 1
        self.log_ui('已安排自动补签：%s（今日第 %d/%d 次）' % (
            self.retry_at.strftime('%H:%M'), self.retry_count, sch['retryMax']), 'warn')
        self.var_footer.set('将在 %s 自动补签未完成账号' % self.retry_at.strftime('%H:%M'))

    def all_done(self):
        done, total = self.count_done()
        return total > 0 and done >= total

    def _tick(self):
        try:
            self._auto_checks()
        except Exception:
            self.log_ui('调度异常：%s' % traceback.format_exc(limit=1), 'err')
        self.after(15000, self._tick)

    def _auto_checks(self):
        now = bj_now()
        today = now.strftime('%Y-%m-%d')
        if self.retry_date != today:
            self.retry_date = today
            self.retry_count = 0
            self.retry_at = None

        try:
            cfg = self.collect_config(strict=False)
        except Exception:
            return
        sch = cfg['schedule']
        hhmm = parse_hhmm(sch['time'], (8, 0))
        sched_today = now.replace(hour=hhmm[0], minute=hhmm[1], second=0, microsecond=0)

        if (not self.running and str(self.cfg.get('lastAutoDate') or '') != today
                and now >= sched_today):
            if self.all_done():
                self.cfg['lastAutoDate'] = today
                self.save_config(silent=True)
                self.log_ui('今日账号均已签到，跳过定时任务', 'dim')
            elif sch['catchUp'] or now < sched_today + timedelta(minutes=15):
                # 只有真的启动成功才记 lastAutoDate；失败留到 5 分钟后再试，
                # 否则一次配置问题就会把当天的自动签到吞掉。
                if (self._auto_fail_at
                        and (now - self._auto_fail_at) < timedelta(minutes=5)):
                    pass
                else:
                    started = self.start_run('每日定时 %02d:%02d' % (hhmm[0], hhmm[1]),
                                             interactive=False)
                    if started:
                        self.cfg['lastAutoDate'] = today
                        self.save_config(silent=True)
                        self._auto_fail_at = None
                        self.log_ui('到达定时时间 %02d:%02d，已开始自动签到'
                                    % (hhmm[0], hhmm[1]), 'head')
                    else:
                        self._auto_fail_at = now
                        self.log_ui('自动签到没能启动（账号或配置不完整），5 分钟后重试', 'err')

        if self.retry_at and not self.running and bj_now() >= self.retry_at:
            self.retry_at = None
            if self.all_done():
                self.log_ui('今日账号均已签到，取消补签', 'dim')
            else:
                hhmm_dl = parse_hhmm(sch['retryDeadline'], (23, 30))
                deadline = bj_now().replace(hour=hhmm_dl[0], minute=hhmm_dl[1],
                                            second=0, microsecond=0)
                if bj_now() >= deadline:
                    self.log_ui('已过补签截止时间，取消补签', 'warn')
                else:
                    self.log_ui('开始自动补签（第 %d 次）' % self.retry_count, 'head')
                    if not self.start_run('自动补签 第%d次' % self.retry_count,
                                          interactive=False):
                        self.retry_at = None
        self._update_chips()

    # ---------- 日志 ----------
    def log_ui(self, message, tag=None):
        stamp = bj_now().strftime('%H:%M:%S')
        line = message if message == '' else '[%s] %s' % (stamp, message)
        self._append_log(line, tag)
        self._file_log(line)

    def log_raw(self, line):
        self._append_log(line, self._tag_for(line))
        self._file_log('[%s] %s' % (bj_now().strftime('%H:%M:%S'), line))

    @staticmethod
    def _tag_for(line):
        if any(k in line for k in ('❌', '失败', '鉴权失败', '异常', '[X]')):
            return 'err'
        if any(k in line for k in ('⚠️', '待重试', '冷却', '读取失败')):
            return 'warn'
        if any(k in line for k in ('✅', '🎉', '成功', '[OK]')):
            return 'ok'
        return None

    def _append_log(self, line, tag=None):
        self.log_text.configure(state='normal')
        self.log_text.insert('end', line + '\n', tag or ())
        self.log_text.configure(state='disabled')
        if self.var_autoscroll.get():
            self.log_text.see('end')

    @staticmethod
    def _file_log(line):
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            path = os.path.join(LOG_DIR, bj_now().strftime('%Y-%m-%d') + '.log')
            with open(path, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
        except Exception:
            pass

    def clear_log(self):
        self.log_text.configure(state='normal')
        self.log_text.delete('1.0', 'end')
        self.log_text.configure(state='disabled')

    def _pump(self):
        try:
            while True:
                kind, payload = self.msgq.get_nowait()
                if kind == 'log':
                    self.log_raw(payload)
                elif kind == 'run_done':
                    self._on_run_done(payload)
                elif kind == 'extract_accounts':
                    self._on_extract_accounts(payload)
                elif kind == 'extract_keys':
                    self._on_extract_keys(payload)
                elif kind == 'web_login_done':
                    self._on_web_login_done(payload)
                elif kind == 'web_url':
                    self._web_url = str(payload or '')
                elif kind == 'switch_done':
                    self._on_switch_done(payload)
                elif kind == 'credits':
                    self._on_credits(payload)
        except queue.Empty:
            pass
        self.after(120, self._pump)

    # ---------- 配置回填 ----------
    def _apply_config_to_widgets(self):
        sch = self.cfg.get('schedule') or {}
        self.var_time.set(str(sch.get('time') or '08:00'))
        self.var_retry.set(bool(sch.get('retryEnabled', True)))
        self.var_retry_min.set(str(sch.get('retryIntervalMin') or 60))
        self.var_retry_max.set(str(sch.get('retryMax') or 6))
        self.var_deadline.set(str(sch.get('retryDeadline') or '23:30'))
        self.var_catchup.set(bool(sch.get('catchUp', True)))
        self.var_startup.set(startup_enabled())

        push = self.cfg.get('push') or {}
        self.var_qywx.set(str(push.get('qywxToken') or ''))
        self.var_plusplus.set(str(push.get('plusplusToken') or ''))

        dev = self.cfg.get('device') or {}
        self.var_dev_mid.set(str(dev.get('machineId') or ''))
        self.var_dev_did.set(str(dev.get('deviceId') or ''))
        self.var_dev_name.set(str(dev.get('name') or ''))
        self.var_dev_model.set(str(dev.get('model') or ''))
        self.var_dev_brand.set(str(dev.get('brand') or ''))
        text_set(self.txt_dev_key, str(dev.get('keyPem') or ''))
        text_set(self.txt_dev_pub, str(dev.get('pubPem') or ''))

    # ---------- 生命周期 ----------
    def _on_tk_error(self, exc, value, tb):
        detail = ''.join(traceback.format_exception(exc, value, tb))
        self.log_ui('界面异常：%s' % detail.strip().splitlines()[-1], 'err')
        messagebox.showerror('程序异常', detail[-1200:], parent=self)

    def _on_close(self):
        if self.running and not messagebox.askyesno(
                '正在签到', '签到还在进行中，确定退出吗？', parent=self):
            return
        if self.switching and not messagebox.askyesno(
                '正在切换账号', '客户端正在切换账号，此时退出可能留下半新半旧的登录态。\n'
                                '确定退出吗？', parent=self):
            return
        if self.web_running:
            if not messagebox.askyesno('正在授权', '浏览器授权还没结束，确定退出吗？',
                                       parent=self):
                return
            self.web_stop.set()
        if self.web_dialog:
            self.web_dialog.close()
            self.web_dialog = None
        self.save_config(silent=True)
        self.destroy()


def main():
    selftest = '--selftest' in sys.argv[1:]
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    app = App(selftest=selftest)
    if selftest:
        app.update()
        app.apply_theme('dark', save=False)
        app.update()
        app.apply_theme('light', save=False)
        app.update()
        print('selftest ok: %dx%d theme=%s' % (
            app.winfo_width(), app.winfo_height(), app.theme_name))
        app.destroy()
        return
    app.mainloop()


if __name__ == '__main__':
    main()
