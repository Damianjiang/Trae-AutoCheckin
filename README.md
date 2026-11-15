# Trae 自动签到 · 多账号管理面板

Trae（Trae CN / TRAE SOLO CN）每日签到、多账号管理、免客户端加号、一键换号。
Windows 桌面面板 + 纯命令行两种用法，零第三方依赖，只要有 Python 3.8+。

**本项目由 AI 开发。**

![Python](https://img.shields.io/badge/Python-3.8%2B-3776ab?logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/Windows-10%2F11-0078d4?logo=windows)
![Deps](https://img.shields.io/badge/dependencies-none-2ea44f)
![License](https://img.shields.io/badge/license-MIT-blue)

---

## 能做什么

| 能力 | 说明 |
| :-- | :-- |
| 每日自动签到 | 到点自动签；启动失败 5 分钟后重试，签不上自动补签，结果落盘可查 |
| 多账号 | 一台电脑管多个号，相邻账号固定错开 1 秒（`TRAE_GAP_SEC` 可调），避开 9074 排队限流 |
| 免客户端加号 | 网页授权登录，本地自造设备身份，不用装/开/关 Trae |
| 一键换号 | 把面板里的任意账号写进客户端并重启，失败自动回滚 |
| 账号绑定 | 每个号绑定到具体客户端（Trae CN / SOLO CN）并带上设备密钥，专治 20403 |
| 推送通知 | 企业微信机器人 / PushPlus，签到结果直达手机 |
| 深浅色面板 | 卡片式界面，深色/浅色一键切换，选择会记住；「刷新」会去服务端拉最新积分 |

---

## 快速开始

### 方式一：图形面板（推荐）

1. 安装 Python 3.8+（安装时勾选 *Add Python to PATH*）；
2. 双击 **`启动签到面板.vbs`**；
3. 「账号列表」→「读取本机账号」，会自动读到你当前登录的号；
4. 点「立即签到」即可，之后可以交给定时任务。

### 方式二：命令行 / 青龙面板

```bash
python trae_checkin.py
```

账号凭据通过 `TRAE_ACCOUNTS` 环境变量传入（JSON 数组），也兼容
`TRAE_REFRESH_TOKEN`、`TRAE_ACCESS_TOKEN`、`TRAE_CONFIG` 等旧写法。

```bash
# 从本机客户端一键生成 TRAE_ACCOUNTS
python trae_get_token.py --accounts

# 只导出设备密钥（换机续期用）
python trae_get_token.py --export-keys
```

---

## 三种添加账号的方式

| 方式 | 适合场景 | 入口 |
| :-- | :-- | :-- |
| 读取本机账号 | 已经在客户端登录过 | 面板「读取本机账号」 |
| 网页登录 | 客户端没登过 / 想加小号 | 面板「批量登录」，或 `python trae_weblogin.py --login 备注名` |
| 手动粘贴 | 从别的机器导出的 refreshToken | 面板「添加账号」 |

网页登录只需要手机号 + 短信验证码，全程在浏览器里完成；面板负责抓取凭据入库，
**不会启动、也不会关闭你正在用的 Trae**。

---

## 账号绑定与 20403

一台机器上 `Trae CN` 和 `TRAE SOLO CN` 可能注册**同一个设备号**，但各自生成不同的
密钥对。只按设备号匹配就会拿到另一份私钥，续期报：

```
refresh http=401 code=20403 Token device not match.
```

现在这么处理：

- 添加/编辑账号时可以指定 **绑定客户端**，并一键「读取该号的设备密钥」；
- 面板启动时会给缺绑定/缺密钥的账号自动补全（优先选「当前正登录着这个号」的客户端）；
- 签到脚本续期时按可信度逐个尝试设备身份，被 20403/10101/20405 拒掉就换下一套，
  签成功的那套会写回账号，之后直接命中。

---

## 换号（冷切换）

「账号列表」选中账号 → 右下「切换到此账号」：

1. 结束正在运行的客户端；
2. 备份当前登录态到 `.trae_switch_backups/`（只留最近 10 份）；
3. 以当前 `storage.json` 为骨架，重写三个身份键并用 Km 加密写回；
4. 拉起客户端，核对落盘 uid 是否为目标账号，**不符自动回滚**。

```bash
python trae_switch.py --status            # 看客户端当前是哪个号
python trae_switch.py --switch 备注名      # 切换
python trae_switch.py --rollback 备份目录  # 手工回滚
```

> 切换会结束客户端进程，未保存的编辑器内容会丢失，操作前请先保存。

---

## 常见状态码

| 码 | 含义 | 处理 |
| :-- | :-- | :-- |
| `9004` / `9095` | 本设备今日已签到 | 正常，不是失败 |
| `9074` | 参与用户太多（排队限流） | 自动换号重签，或等冷却后补签 |
| `20403` | token 与设备不匹配 | 见上一节「账号绑定」 |
| `20405` | 缺少设备证明 | 补设备密钥：`python trae_get_token.py --export-keys` |
| `10101` / `20101` | refreshToken 不匹配 / 已失效 | 重新登录该账号 |

---

## 目录结构

```text
Trae-AutoCheckin/
├── trae_gui.py             # 桌面面板
├── 启动签到面板.vbs         # 双击启动（自动找 Python）
├── trae_checkin.py         # 签到主脚本（命令行 / 青龙 / Actions）
├── trae_weblogin.py        # 网页登录加号（免客户端）
├── trae_switch.py          # 换号（冷切换 + 回滚）
├── trae_km.py              # storage.json 的 Km 编解码
├── trae_autologin.py       # 隔离配置目录启动客户端（备用方案）
├── trae_get_token.py       # 本机凭据 / 设备密钥提取
├── trae_sms_login.py       # 独立网页 OAuth 工具
├── trae_credit_monitor.py  # 积分只读监控
├── 配置生成器/              # 可视化生成 TRAE_CONFIG
└── tests/                  # 离线回归测试
```

运行期生成的文件（账号配置、token 缓存、备份、设备私钥）都已加入 `.gitignore`，
不会进入仓库。

---

## 关键词

Trae、Trae CN、TRAE SOLO、自动签到、每日签到、积分、积分领取、多账号、多开、账号切换、
换号、免登录、网页授权、OAuth、PKCE、refreshToken、accessToken、设备身份、设备指纹、
deviceId、20403、20405、9074、青龙面板、GitHub Actions、Windows 桌面工具、Python、
tkinter、自动化脚本、Trae 助手、IDE 工具。

---

## 免责声明

本项目仅供个人学习与自动化实践使用。请遵守 Trae 官方的用户协议；因使用本工具导致的
账号异常、积分变动或其它后果，由使用者自行承担。请勿用于商业用途或批量注册。

## License

[MIT](LICENSE)
