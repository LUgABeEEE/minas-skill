# 使用说明

完整文档见仓库根目录 [README.md](../README.md)。

## 最短上手

1. 在官方客户端开启 Samba 共享  
2. 配置 `MINAS_HOST`，并**强烈建议**设置 `MINAS_ROOT`（把所有操作锁在该 UNC 前缀内）；  
   凭据按 `MINAS_PASSWORD_FILE`（推荐，优先于交互提示）→ 交互 `*` → `MINAS_PASS`（不推荐）的顺序提供  
3. 运行：

```powershell
python scripts/minas.py shares --host <NAS_IP>
python scripts/minas.py ls //<NAS_IP>/<共享名>
```

## Agent 必读

见 [SKILL.md](../SKILL.md) 中的「Agent 行为约束」。

## 示例

见 [references/examples.md](../references/examples.md)。
