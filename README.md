# 小米智能存储 Samba 文件助手 · 使用文档

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

面向 **Agent / 脚本** 的小米智能存储（Xiaomi Smart Storage，内部代号 MINAS）文件工具。  
通过 **Samba（SMB/CIFS）** 列目录、统计空间、上传下载、整理文件，无需挂载盘符。

> 非小米官方项目。请只在你自己拥有或已授权的设备上使用。

---

## 1. 它能做什么

| 功能 | 命令 |
|---|---|
| 发现共享 | `shares` |
| 列目录 | `ls` |
| 看体积 | `du` / `df` / `tree` |
| 传文件 | `get` / `put` / `cat` |
| 整理 | `mkdir` / `mv` / `cp` / `rm` / `find` |

**不包含**：SSH、root、Docker、固件修改、挂载盘符守护进程。

---

## 2. 环境要求

- Windows 10/11（主路径使用系统 SMB 客户端）
- 小米智能存储与电脑在同一局域网
- 官方客户端里已开启 Samba 共享（能看到 `\\主机\共享名`）
- Python 3.10+（仅用于运行 CLI）

---

## 3. 配置

推荐用环境变量，**不要写进代码或仓库**：

```powershell
$env:MINAS_HOST = "<NAS 的 IP 或主机名>"
$env:MINAS_SHARE = "<共享名>"

# 限制所有文件操作只允许该 UNC 前缀（不设置则允许任意 UNC，
# 但一律拒绝本地路径如 C:\...，且拒绝用 ../ 逃逸出该前缀）
$env:MINAS_ROOT = "\\192.168.x.x\共享名"

# 若 Windows 没有现成会话再设置：
$env:MINAS_USER = "<SMB 用户名>"
```

**密码三选一（按优先级）**：

1. **密码文件（推荐，交互与 agent 都适用）**：

   ```powershell
   Set-Content -Path .\minas_pw.txt -Value "<SMB 密码>" -NoNewline
   # 收紧权限：仅当前用户可读
   icacls .\minas_pw.txt /inheritance:r /grant:r "$env:USERNAME`:F"
   $env:MINAS_PASSWORD_FILE = ".\minas_pw.txt"
   ```

   设置后即使在可交互终端也会走文件；密码经 Win32 `WNetAddConnection2W`
   传入，**不进进程命令行**。

2. **交互提示（未设密码文件时）**：在可交互终端运行时以 `*` 方式提示输入，
   密码不进命令行、不进环境变量；

3. **环境变量（最后兼容，不推荐）**：`$env:MINAS_PASS = "<SMB 密码>"` —— 脚本会打印
   安全警告（环境变量可能被子进程/转储读到）；同样不经过 `net use` 的 argv。

也可以只用当前 Windows 已认证的 SMB 会话（资源管理器能打开共享即可）。

---

## 4. 快速开始

```powershell
# 1. 看有哪些共享
python scripts/minas.py shares --host 192.168.x.x

# 2. 列出共享内容（把 host/share 换成你的）
python scripts/minas.py ls //192.168.x.x/共享名

# 3. JSON 输出（给程序用）
python scripts/minas.py ls //192.168.x.x/共享名 --json

# 4. 统计一层目录大小
python scripts/minas.py du //192.168.x.x/共享名 --max-depth 1
```

Windows UNC 也可以：

```powershell
python scripts/minas.py ls \\192.168.x.x\共享名
```

---

## 5. 命令详解

### 5.1 `shares` — 列出共享

```powershell
python scripts/minas.py shares --host 192.168.x.x
python scripts/minas.py shares --host 192.168.x.x --json
```

### 5.2 `ls` — 列目录

```powershell
python scripts/minas.py ls //HOST/SHARE
python scripts/minas.py ls //HOST/SHARE/folder --json
```

### 5.3 `du` — 目录体积

```powershell
python scripts/minas.py du //HOST/SHARE --max-depth 2
```

**务必限制 `--max-depth`**，避免扫超大目录卡住。

### 5.4 `tree` — 树形结构

```powershell
python scripts/minas.py tree //HOST/SHARE --max-depth 2
```

### 5.5 `df` — 容量

```powershell
python scripts/minas.py df //HOST/SHARE
python scripts/minas.py df //HOST/SHARE --json
```

### 5.6 `get` / `put` / `cat` — 传文件

```powershell
python scripts/minas.py get //HOST/SHARE/a.txt .\a.txt
python scripts/minas.py put .\a.txt //HOST/SHARE/backup/a.txt
python scripts/minas.py cat //HOST/SHARE/notes.txt
```

### 5.7 `mkdir` / `mv` / `cp` / `rm`

```powershell
python scripts/minas.py mkdir //HOST/SHARE/work
python scripts/minas.py mv //HOST/SHARE/a.txt //HOST/SHARE/work/a.txt
python scripts/minas.py cp //HOST/SHARE/a.txt //HOST/SHARE/a.bak

# 删除/移动前先预览（只打印计划，不改动任何文件）
python scripts/minas.py rm //HOST/SHARE/work --recursive --dry-run
python scripts/minas.py rm //HOST/SHARE/work --recursive
python scripts/minas.py mv //HOST/SHARE/a.txt //HOST/SHARE/b.txt --dry-run
```

`rm --recursive` 删除目录前，请先确认用户同意；建议先 `--dry-run` 看一眼再动手。

### 5.8 `find` — 查找

```powershell
python scripts/minas.py find //HOST/SHARE --name "*.jpg" --max-depth 3 --files
python scripts/minas.py find //HOST/SHARE --name "*备份*" --json
```

---

## 6. 给 Agent 的推荐流程

```text
1. shares          → 有哪些共享
2. ls / tree       → 结构
3. du --max-depth  → 哪里占空间
4. 明确要做什么    → 需要时向用户确认
5. get 到本地工作区 → 处理 → put 回去
6. 大范围删除前再次确认
```

**退出码**

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 一般错误 / 参数问题（含路径不合法、越过 `MINAS_ROOT`） |
| 2 | 路径不存在（主机与共享可达） |
| 3 | 权限或网络问题（含主机/共享不可达——此时**不要**当作「文件不存在」） |

---

## 7. 常见问题

**Q: 看不到共享？**  
先在资源管理器地址栏输入 `\\NAS_IP\` 看能否浏览；到官方 App/客户端检查「Samba / 共享」是否开启。

**Q: 提示 1219（多重凭据）？**

```powershell
net use * /delete /y
```

然后只用一种账号重新访问。

**Q: 中文共享名/文件名乱码？**  
请使用与资源管理器中显示一致的名称；CLI 不会擅自改名。

**Q: 和 WebDAV 有什么区别？**

| | Samba（本工具） | WebDAV |
|---|---|---|
| 端口 | 445 | 通常 5000 |
| 适合 | 局域网批量文件 | 远程、部分分享 |
| 本工具 | **默认** | 非本版重点 |

**Q: 能不能挂成盘符？**  
可以自己用 `net use Z: \\NAS\share`，但本 Skill 不托管挂载，避免开机项和凭据残留。

---

## 8. 安全须知

1. 只在自有/授权设备上使用  
2. 密码优先用交互提示或 `MINAS_PASSWORD_FILE`；不要把 `MINAS_PASS`、完整账号写进 Git、笔记或聊天记录  
3. 公开分享仓库前确认 `.gitignore` 已排除 `*.pem` / `.env` / 密码文件  
4. 对不可恢复的删除操作保持谨慎  
5. **路径根约束**：所有远端路径必须是 UNC（`\\主机\共享\...`），本地路径（`C:\...`）
   一律拒绝；设置 `MINAS_ROOT` 后，路径规范化（去掉 `../`）仍必须落在该前缀之下，
   `rm --recursive` 等删除操作同样受此约束  

---

## 9. 目录结构

```text
minas-skill/
  SKILL.md                 # Agent 技能说明
  README.md                # 本使用文档
  scripts/minas.py         # CLI
  tests/test_minas.py      # 离线单测（路径安全 / 退出码 / dry-run）
  references/
    samba-surface.md       # 探测结论
    examples.md            # 示例
  docs/USAGE.md            # 与 README 同步的使用说明
  locales/zh-CN.json       # 技能显示元数据
  .github/workflows/       # gitleaks 密钥扫描 + 单测 CI
  .gitignore
```

**运行测试**（纯离线，不触网）：

```powershell
python -m unittest discover -s tests -v
```

---

## 10. 反馈与限制

- 当前以 **Windows SMB 客户端** 为主路径  
- Linux/macOS 可改用 `smbclient` / `mount.cifs`（文档未展开）  
- 共享范围以小米智能存储里实际开启的 Samba 共享为准  
- Bug / 需求请用 [Issue 模板](.github/ISSUE_TEMPLATE)；安全问题见 [SECURITY.md](SECURITY.md)
- 贡献流程见 [CONTRIBUTING.md](CONTRIBUTING.md)；社区行为准则见 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)
- 版本记录见 [CHANGELOG.md](CHANGELOG.md)

---

## 11. 开源许可与声明

本项目基于 [Apache License 2.0](LICENSE) 开源，版权声明与商标说明见 [NOTICE](NOTICE)。

```text
Copyright 2026 missfor100
SPDX-License-Identifier: Apache-2.0
```

**声明**

- 本项目是面向小米智能存储（MINAS）的**第三方**文件操作工具，**不是**小米官方产品，与 Xiaomi 无隶属、赞助或背书关系。
- Xiaomi、小米智能存储及相关商标归其各自权利人所有；此处仅用于指明兼容目标设备。
- 软件按“现状”提供，不附带任何明示或默示担保；使用本工具访问你自己的 NAS 的风险自负。请遵守设备所在地法律法规与厂商服务条款。
- 请勿将本工具用于未授权访问他人存储设备。

祝整理愉快。
