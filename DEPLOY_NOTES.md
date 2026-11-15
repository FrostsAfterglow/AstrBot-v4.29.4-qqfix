# AstrBot 部署笔记（沙盒 / proot 环境）

## 当前状态
- 版本：**AstrBot 4.29.4**（源码 commit `630d5ce`，`git clone --depth=1`）
- 目录：`/workspace/AstrBot`
- Python：`.venv`（系统 Python 3.12.3 + uv 0.13.8），150 个依赖已装好，`uv pip check` 无冲突
- WebUI：预编译 dist 已放入 `data/dist`（`assets/version` = `v4.29.4`，与内核匹配），**未**本地编译
- 面板端口：**6186**（6185 被设备上其它进程占用）
- 面板账号：`astrbot` / `Astrbot123`（pbkdf2 写入 `data/cmd_config.json`，已关闭强制改密）
- 启动验证：HTTP 200、`POST /api/auth/login` 返回 JWT、日志无报错；启动约 50~70s

## 启动方式
```bash
cd /workspace/AstrBot
./start.sh                  # 前台运行
# 或
./serve.sh                  # 同上，日志写到 /tmp/astrbot.log
```
浏览器打开 `http://127.0.0.1:6186`（proot 与 Android 共用网络命名空间，手机浏览器可直接访问）。

## 环境坑位（复现必看）
1. **uv 硬链接失败**：proot 的 `link2symlink` 不支持硬链接，报
   `failed to hardlink file ... Operation not permitted (os error 1)`
   → 加 `--link-mode=copy`（env: `UV_LINK_MODE=copy`）。
2. **后台进程活不过一条命令**：proot 以 `--kill-on-exit` 启动，命令结束后所有子进程被杀。
   `nohup` / `setsid` / `disown` 都没用 → 服务必须前台运行（或靠长会话维持）。
3. **uv 会中途被杀**：依赖必须前台装（可分段重跑，uv 有缓存会续传）。
4. **下载源**：pip 用清华源；npm 用 `registry.npmmirror.com`；GitHub release 直连很慢（~11KB/s），
   但 `https://astrbot-registry.soulter.top/download/astrbot-dashboard/v<ver>/dist.zip` 很快（~360KB/s）。
   注意该地址 **必须带 `v` 前缀**，`4.29.4` 会 404。
5. **dist.zip 内层还有一层 `dist/`**，解压后要把内容上移一级，`data/dist/index.html` 才是正确位置。
6. **端口占用**：`/proc/net/tcp` 不可读，AstrBot 报"占用信息获取失败"属正常，需自己用 socket bind 探测。
   `data/cmd_config.json` → `dashboard.port` 改端口。
7. **改密码**：`astrbot.core.utils.auth_password.hash_dashboard_password()` +
   `hash_md5_dashboard_password()` 同时写入 `pbkdf2_password` 和 `password` 两个字段。

## 可选：自己编译 WebUI（当前未使用）
Node 22.20.0 在 `/workspace/.tools/node`，pnpm 9.15.9 已全局安装：
```bash
export PATH=/workspace/.tools/node/bin:$PATH
cd /workspace/AstrBot/dashboard
pnpm install --ignore-workspace --registry=https://registry.npmmirror.com
node scripts/subset-mdi-font.mjs && npx vite build   # 跳过 vue-tsc 省内存
```
注意：本机到 npm registry 很慢（单包 ~10s 超时重试），全量安装预计 30min+，故改用预编译包。
`allowBuilds` 是 pnpm 10.16+ 的字段，pnpm 9 会忽略；workspace 文件缺 `packages` 字段必须加 `--ignore-workspace`。
