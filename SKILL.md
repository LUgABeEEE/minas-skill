---
name: minas
description: 小米智能存储（Xiaomi Smart Storage / MINAS）Samba/SMB 文件助手：列共享、列目录、体积统计、上传下载、整理 NAS 文件。当用户提到 小米 NAS / 小米智能存储 / MINAS / 私有云 / Samba / SMB / 共享文件夹 / \\主机\共享名，或需要列出、统计、拷贝、改名、删除 NAS 上的文件时使用。仅做文件层操作，不涉及 SSH/root/Docker/固件。
---

# minas

> 小米智能存储（Xiaomi Smart Storage / MINAS）Samba 文件助手  
> 让 agent 用 SMB/CIFS 快速列目录、统计、上传下载、整理 NAS 文件。

## 何时使用

用户提到以下任意情况时加载本技能：

- 小米 NAS / 小米智能存储 / MINAS / 私有云存储
- 处理 NAS 上的照片、文档、分享目录
- 需要列出 / 统计 / 拷贝 / 改名 / 删除 NAS 文件
- Samba / SMB / 共享文件夹 / `\\主机\共享名`

**不要**用于：刷机、开 SSH/root、Docker、固件破解（本技能只做文件层）。
需要 `/pool0/data` 全池（含未发布为 Samba 共享的目录）请改用 skill `minas-webdav`。

## 前置条件

1. NAS 与本机同一局域网
2. 已在官方客户端里开启/看到 Samba 共享
3. **按平台**能连上共享：

| 平台 | 连接方式 | 额外要求 |
|---|---|---|
| Windows | 系统原生 UNC（`net use` 建立会话） | 能访问 `\\<NAS>\<共享名>`，或提供账号密码 |
| Linux / macOS | `smbclient` 子进程，**不挂载** | 安装 Samba 客户端：`apt install smbclient`（Debian/Ubuntu）／`brew install samba` |

> POSIX 上每次调用都会起一个 `smbclient`，因此比 Windows 稍慢；`ls` 的目录元数据在单次进程内缓存，`du` 的逐层递归仍是一次调用一层。

可选环境变量（**不要把密码写进对话或仓库**）：

```text
MINAS_HOST           NAS 的 IP 或主机名
MINAS_SHARE          默认共享名
MINAS_USER           SMB 用户名
MINAS_ROOT           限制所有远端操作只能落在该 UNC 前缀之下（强烈建议设置）
MINAS_PASSWORD_FILE  密码文件路径（推荐；设置后优先于交互提示）
MINAS_PASS           SMB 密码（最后兼容，会打印安全警告，优先用密码文件）
```

## 快速用法

```bash
# 列出共享
python scripts/minas.py shares --host <NAS_IP>

# 列目录
python scripts/minas.py ls //<NAS_IP>/<共享名>

# 结构化输出（推荐 agent 使用）
python scripts/minas.py ls //<NAS_IP>/<共享名> --json

# 体积统计（限制深度，禁止无脑全盘递归）
python scripts/minas.py du //<NAS_IP>/<共享名> --max-depth 2

# 上传 / 下载
python scripts/minas.py get //<NAS_IP>/<共享名>/a.txt ./a.txt
python scripts/minas.py put ./a.txt //<NAS_IP>/<共享名>/a.txt

# 整理
python scripts/minas.py mkdir //<NAS_IP>/<共享名>/work
python scripts/minas.py mv //.../a.txt //.../work/a.txt
python scripts/minas.py rm //.../work --recursive --dry-run   # 先预览，不改动
python scripts/minas.py rm //.../work --recursive
```

路径也可用 Windows UNC：`\\NAS\share\file`。两种写法（`//host/share/...` 与 `\\host\share\...`）在两个平台都接受。

**换环境后先自检**（POSIX 后端解析 `smbclient` 文本输出，先确认能正确解析你的服务器）：

```bash
python scripts/minas.py selftest //<NAS_IP>/<共享名>          # 只读检查
python scripts/minas.py selftest //<NAS_IP>/<共享名> --raw    # 附带原始 smbclient 输出
```

退出码：`0` 成功 · `1` 一般错误/参数问题 · `2` 路径不存在（主机可达） · `3` 权限、网络或主机/共享不可达。

## Agent 行为约束（必须遵守）

1. **先探后动**：先 `shares` / `ls` / `du --max-depth`，看清结构再写。  
2. **大目录禁止无限递归**：`du` / `find` / `tree` 必须带 `--max-depth`。  
3. **删除必须征得用户同意**：任何 `rm --recursive`、批量删照片/备份目录前先确认；`rm` / `mv` 支持 `--dry-run`，动手前先预览（不改动任何文件）。  
4. **批量重处理先拉本地**：`get` 到工作目录 → 本地处理 → `put` 回去。  
5. **不落敏感信息**：账号、密码、完整设备 ID 不写入项目文件、日志、对话。  
6. **不改系统**：不装服务、不动注册表、不碰 SSH/Docker。

## 实现说明

| 文件 | 作用 |
|---|---|
| `scripts/minas.py` | Samba CLI 主体（路径校验、命令、输出格式）——两平台共用 |
| `scripts/minas_smb.py` | **POSIX 后端**：`SmbPath`（pathlib 风格门面）+ `shutil` 替代（`copy2`/`copytree`/`move`/`rmtree`/`disk_usage`），底层调 `smbclient` |
| `tests/test_minas.py` | 离线单测（路径安全约束 / 退出码 / dry-run） |
| `references/samba-surface.md` | 协议与共享探测结论 |
| `references/examples.md` | 常见任务示例 |
| `docs/USAGE.md` | 完整使用文档 |

**平台分派**：Windows 走系统 UNC（`net use` + `pathlib`/`shutil`）；Linux/macOS 走 `smbclient` 子进程。命令层代码两平台共用，差异集中在 `to_local()`、`ensure_session()`、`list_shares()` 与 `fs_*` 这几个分派点。

不依赖任何第三方 Python SMB 库；POSIX 侧依赖系统 `smbclient` 可执行文件。

## 故障排查

| 现象 | 处理 |
|---|---|
| 找不到共享 | `python scripts/minas.py shares --host <IP>`；到官方客户端确认共享已开启 |
| 1219 多重凭据 | （Windows）`net use * /delete /y` 后重连，只用一种账号 |
| 59 / 拒绝访问 | 核对共享名（中文共享名需原样使用）与账号 |
| 路径不存在 | 确认是 `//host/share/...` 而不是 `/pool0/...`（那是 WebDAV 路径） |
| `smbclient not found`（Linux） | `apt install smbclient`；本技能不挂载、不需要 `cifs-utils` |
| `cannot parse smbclient listing output`（Linux） | `selftest <路径> --raw` 看原始输出，把该输出反馈给维护者（说明服务器/版本的 `ls` 格式与预期不符） |
| `df` 在 Linux 上报无可用空间 | SMB 侧容量来自 `smbclient du` 的 footer；若服务器不返回，改用 `minas-webdav df` 查存储池容量 |

## 安全边界

- 仅操作**用户自己的** NAS 共享  
- 本仓库与 Skill **不含**任何 IP、账号、密码、证书  
- 本地密钥/密码请放在环境变量或系统凭据管理器，勿提交 Git
