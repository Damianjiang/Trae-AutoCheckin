# 更新日志

## 未发布（2026-11-15）

- 新增「账号绑定」：添加/编辑账号时可指定绑定到哪个客户端（Trae CN / TRAE SOLO CN），并一键读取该号在本机的设备密钥；面板启动时自动为缺绑定/缺密钥的账号补全。
- 面板：账号列表新增「绑定客户端」列；切换账号按账号绑定选择客户端，不再固定 Trae CN。
- 整理：精简模块头注释与装饰性分隔线，移除与外部项目的对照说明；删除多语言 README、打赏二维码与使用边界声明；重写 README。
- 修复：**同一设备号、不同客户端导致续期 20403 `Token device not match`**。Trae CN 与 TRAE SOLO CN 会注册同一个设备号却各有一套密钥，旧逻辑只按设备号匹配，取到了另一台客户端的私钥，续期必然被拒。现在 `identity_candidates()` 会列出所有候选身份（环境变量 > 账号自带 > 本机正在登录该号的身份 > 同设备号身份），`ensure_token()` 逐个尝试，被 20403/10101/20405 拒掉就换下一套，签成功的那套会回写进账号，后续直接命中。
- 修复：`trae_km` 不再 `from trae_checkin import ...`（会把签到脚本当第二个模块再加载一次，重复执行配置层并打印 "no usable fields" 噪音），AES 解密改为自带实现，并与 `trae_get_token` 交叉校验。
- 面板：签到结束后自动把续期轮换后的新 token 合并回配置，避免「切换客户端账号」时把已失效的 refreshToken 写进客户端。
- 实测：原先报 20403 的账号已能正常签到，复跑均为「今日已签到，跳过」。
- 新增 `trae_weblogin.py`：**免客户端的网页登录**。自造设备身份（16 位设备号 + P-256 密钥对，纯标准库导出 PKCS#8/SPKI PEM），走官网 OAuth 回环换 `refreshToken`，无需安装或启动 Trae。17388 被占时自动换端口（回调 URI 带真实端口）。
- 新增 `trae_km.py`：`storage.json` 的 Km 编解码，含 AES-128 加密方向（FIPS-197 向量校验）。密钥调度与机器无关，因此可离线写出「客户端认得的登录态」。
- 新增 `trae_switch.py`：**一键换号（冷切换）**。结束客户端 → 备份载体 → 以现有 `storage.json` 为骨架重写 `icube.cloudide` / `usertag` / `icube-dc:*` 三键并抹掉旧号权益快照 → 拉起客户端 → 核对落盘 uid，不符自动回滚。
- 新增 `trae_autologin.py`：隔离配置目录启动客户端并精确关闭（只认自己的 `--user-data-dir` 进程树），作为换号兜底方案；子进程环境会剔除 `ELECTRON_RUN_AS_NODE` 等会污染 Electron 的变量。
- 面板：新增「批量登录」页（逐号网页授权 + 自动入库 + 可选自动签到）；账号列表新增「切换到此账号」与客户端当前账号显示。
- 面板：老账号只存 `refreshToken` 时，换号会按设备号到客户端里取回密钥对，无需重新登录。
- 测试：新增 `tests/test_switch_km.py`（17 项：AES 向量、Km 往返与篡改检测、三键合成、密钥回退、载体探测、PKCE 设备、隔离目录），原有 23 项回归继续通过。
- 说明：本轮未执行真实换号与真实登录；Km 合成、密钥导出与关闭流程均已离线验证。

## v1.1.0（2026-11-13）

- 新增 `TRAE_CONFIG` 统一 JSON 配置层：签到与积分监控脚本都可从单变量读取账号、设备、推送和调度配置，同时继续兼容原有环境变量。
- 新增 Windows「配置生成器」：支持读取本机 Trae 凭据、导入旧配置、生成 `TRAE_CONFIG`，并保留账号级设备证明、`realDeviceId`、`icubeAuth` 和 `storagePath` 字段。
- `refreshToken-only` 账号现在可直接加载，并在首次运行时走原有自动换 AT 流程；签到脚本的续期设备证明和积分监控均优先使用账号级 `realDeviceId`。
- 安全加固：配置适配日志只输出变量名，不输出凭据值或前缀；Windows GBK 控制台不再因新日志崩溃。
- 配置页下载敏感 JSON 需要二次确认，清空表单同步清空导入框、账号、设备名称/型号/品牌和旧凭据。
- 本地 relay 增加 Host/Origin/Referer 校验、`X-Frame-Options: DENY`、CSP `frame-ancestors 'none'`、一次性提取令牌和多标签页租约；跨站 iframe 无法触发凭据提取或关闭中继。
- Windows 重复启动复用同一实例；端口探测移除 `SO_REUSEADDR`，HTTP server 使用原子 bind，避免被已有监听器误导。
- 启动 VBS 改为可审阅的 ASCII + CRLF 文本，并在 `.gitattributes` 中固定文本属性。
- 测试：新增配置层、GBK、导入导出、relay 生命周期、iframe、双标签、端口占用和 Windows 启动回归；`55` 项测试及子测试通过，Python/Node 语法、敏感扫描与 `git diff --check` 通过。
- 说明：本轮未调用真实 Trae 刷新或签到接口，未读取真实 Trae storage，未包含账号、Token 或设备凭据。

## v1.0.9（2026-11-04）

- 修复：签到状态解析只读取 `credits`。当服务端返回 `credits=0`、`extra_credits>0` 时，本地会把本次签到积分误显示为 0。
- 行为：新增统一的 `reward_credits()` 解析入口；`credits > 0` 时使用 `credits`，否则使用正的 `extra_credits`。两个字段是并列分量，绝不相加；缺失、字符串、布尔和负数等异常值不会进入展示。
- 测试：新增 7 项离线回归，覆盖 `credits > 0`、`credits=0 + extra_credits>0`、两者并存、缺失/字符串/负数异常、状态查询及报告路径；测试总数 23 项，`py_compile`、`unittest`、`git diff --check` 通过。
- 说明：本轮未使用真实账号，未发送真实签到请求。

## v1.0.8（2026-10-27）

- 修复：**`TRAE_BATCH=<数字>` 必崩** —— `pick_batch()` 在非早窗时段取用了从未定义的 `size`，只要把 `TRAE_BATCH` 设成 `1` / `2` / 任意数字，且当前不在早窗时段，整轮签到直接 `NameError` 退出。
  - 默认 `all` 与早窗全签两条路径提前 return，所以默认配置下从未暴露；
  - 该行是「早窗全签」快路径重构时被误删的，`git log -S size` 可追到 `4ca5697` 的原始实现；
  - 现按原实现恢复 `size = max(1, int(raw))`，并对非法值（如 `TRAE_BATCH=abc`）降级为 1，不再抛错打断整轮签到。
- 测试：新增 8 项 `pick_batch` 离线回归测试（数量截断、超量收敛、非法值降级、`all` 全签、早窗全签、北京时间轮换、空列表），全部通过；`py_compile` 与 `ruff --select F821` 无告警。
- 说明：本轮未使用真实账号，未发送真实签到请求。

## v1.0.7（2026-10-27）

- 修复：签到领取接口的 `9004` 与 `9095` 都表示「本设备今日已签到」，现在统一按已签成功处理，不再把 `9004` 误判为失败。
  - 覆盖 `claim_with_retry` 与 `checkin_account` 两处分类；
  - 新增 4 项离线回归测试，分别验证 9004 / 9095 在领取与账号结果中的分类。
- 增强：积分监控新增只读接口 `POST /trae/api/v2/pay/ide_user_ent_usage`（请求体 `{"require_usage":true}`）。
  - 解析 `user_entitlement_pack_list[]` 的 `expire_time`、`quota.credits_limit`、`usage.credits_amount`；
  - 自动跳过已耗尽积分包，按到期时间排序并显示「最早到期包」；
  - 查询失败时不影响原有余额/已用查询，签到主流程完全不受影响。
- 测试：新增 8 项离线回归测试，全部通过；`compileall` 与 `git diff --check` 通过。
- 说明：本轮未使用真实账号，未发送真实签到请求。

## v1.0.6（2026-10-21）

- 新增：GitHub Actions 工作流 `.github/workflows/daily.yml`（默认只开手动触发，schedule 需 fork 后自行取消注释）；
  - 用 Actions Cache 持久化 `.trae_token_cache.json` / `.trae_device_ids.json` / `.trae_checkin_state.json`；
  - 缓存内容用 `TRAE_CACHE_KEY` 加密，避免轮换后的 refreshToken 以明文进入 Cache；
  - 解决 Actions 每次全新环境导致 refreshToken 轮换后下一次运行失效的问题。
- 兼容：PushPlus token 继续优先识别 `PLUSPLUS_TOKEN`，同时支持 `PLUSHPLUS_TOKEN` / `PUSHPLUS_TOKEN` 别名；
- 文档：README 补充 Actions 部署步骤、Secrets 清单与 `TRAE_CACHE_KEY` 说明。


## v1.0.5（2026-10-16）

- 修复：**中国版账号的令牌到期读不出来**（与上游 issue #263 同款问题）。
  - 中国版 JWT 把到期写在自定义声明 `TokenExpireAt`（且可能是**字符串**），旧的只认标准 `exp`，读出来是 0 —— 缓存里的有效期与自动续期节奏一起坏，且不报任何错；
  - 现在按 `exp` → `TokenExpireAt` → `tokenExpireAt` 顺序读取，数字 / 字符串都认，秒 / 毫秒按量级归一；
  - `trae_checkin.py` 与 `trae_credit_monitor.py` 两处同步修正。

## v1.0.4（2026-10-13）

- 修复：签到结束后**逐账号明细也打印到终端**（此前只进推送，没配推送时终端只有「失败 1」这种计数，看不出原因）。排障请直接看这几行。

## v1.0.3（2026-10-13）

- 新增：**领取后同设备号回查确认** —— claim 返回 `code:0` 不再直接判成功（当日已签过的账号对任何 device_id 都回 0，属于假成功）。现在以同设备号回查 `checked_in` 是否翻转为准，未确认就不报成功、交给下一轮 cron，避免亮绿灯但实际没到账。
- 新增开关 `TRAE_VERIFY_CLAIM`（默认 `1`），设 `0` 可关闭该回查。

## v1.0.2（2026-10-09）

- 修正对 9074（「当前参与用户太多」）的理解与处理：它绝大多数不是限流，而是**设备号不对**。
  实测同账号同时刻对照：脚本自动生成的设备号 → `9074`，客户端真实注册号 → 通过设备检查。
- `x-device-id` 一律优先使用客户端真实注册过的设备号（storage.json 里
  `iCubeAuthInfo://icube-dc:<数字>` 键名中的那串数字）。
- 设备号长度校验放宽为 12~20 位十进制：15 位的真实号不再被丢弃（16 位只是观察，不是规则）。
- 命中 9074 后不再生成假号重试，只在**真实设备号之间**切换；换不到就冷却等下轮 cron。
- `trae_get_token.py --accounts` 现在会给每个账号自动带上 `realDeviceId`。
- `trae_credit_monitor.py` 同样优先使用客户端真实设备号。

## v1.0.1（2026-10-09）

- 启动即拦截 token 错配（accessToken / refreshToken 填成同一个值或填反）。
- TRAE_ACCOUNTS JSON 解析失败时给出更友好的格式示例。
- README 补充「鉴权失败（硬失败）」排查说明。

## v1.0.0（2026-10-04）

- 首个公开版本：多账号每日签到、token 自动续期、9074 处理、企业微信 / PushPlus 推送。
