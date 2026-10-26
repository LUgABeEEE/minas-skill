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

## 前置条件

1. NAS 与本机同一局域网
2. 已在官方客户端里开启/看到 Samba 共享
3. Windows 能访问 `\\<NAS>\<共享名>`（已有会话，或提供账号密码）

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

路径也可用 Windows UNC：`\\NAS\share\file`。

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
| `scripts/minas.py` | Samba CLI（Windows UNC / `net use`） |
| `tests/test_minas.py` | 离线单测（路径安全约束 / 退出码 / dry-run） |
| `references/samba-surface.md` | 协议与共享探测结论 |
| `references/examples.md` | 常见任务示例 |
| `docs/USAGE.md` | 完整使用文档 |

底层使用系统 SMB 客户端与 UNC 路径，不依赖第三方 SMB 库。

## 故障排查

| 现象 | 处理 |
|---|---|
| 找不到共享 | `python scripts/minas.py shares --host <IP>`；到官方客户端确认共享已开启 |
| 1219 多重凭据 | `net use * /delete /y` 后重连，只用一种账号 |
| 59 / 拒绝访问 | 核对共享名（中文共享名需原样使用）与账号 |
| 路径不存在 | 确认是 `//host/share/...` 而不是 `/pool0/...`（那是 WebDAV 路径） |

## 安全边界

- 仅操作**用户自己的** NAS 共享  
- 本仓库与 Skill **不含**任何 IP、账号、密码、证书  
- 本地密钥/密码请放在环境变量或系统凭据管理器，勿提交 Git
