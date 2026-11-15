# AstrBot v4.29.4 · QQ 图片/文件获取失败修复

## 一、问题现象

线上日志反复出现：

```
[ERRO] [utils.io:188]: Failed to download file from https URL
       host='gchat.qpic.cn' file='download' len=223. HTTP status code: 400
[WARN] [preprocess_stage.stage:185]: Image processing failed for https URL
       host='gchat.qpic.cn' file='download' len=223: ... HTTP status code: 400
```

群成员发图片时，AstrBot 无法取得图片内容，LLM 侧只看到空图片/处理失败。

## 二、根因

1. **下载请求不带任何请求头**
   `astrbot/core/utils/io.py` 的 `download_file()` / `download_image_by_url()`
   直接 `session.get(url)`，aiohttp 默认 UA 是 `Python/3.x aiohttp/x`。
   QQ CDN（`gchat.qpic.cn`、`qfile.qq.com`、`multimedia.nt.qq.com.cn` 等）
   对非浏览器 UA 返回 **400**。

2. **QQ 私有 CDN 链接需要登录态**
   协议端（NapCat/LLOneBot）对部分群图片给出的是
   `https://gchat.qpic.cn/download?appid=...&fileid=...` 形式，
   这类链接依赖 QQ 会话 Cookie，第三方直接 GET 同样 400。
   正解是走 OneBot 的 `get_image` 让协议端自己取图。

3. **链接未清洗**
   平台偶尔回传 HTML 转义（`&amp;`）、带引号或零宽字符的 URL，签名参数被破坏 → 400。

## 三、修复内容

### 1. `astrbot/core/utils/io.py`

- 新增 `normalize_download_url()`：HTML 反转义、去引号/零宽字符/换行、补全 `//`。
- 新增 `build_download_headers()` / `download_header_candidates()`：
  - 浏览器 UA + `Accept` + `Accept-Language`；
  - 腾讯系域名自动带 `Referer`；
  - 400/403 时依次尝试
    `qun.qq.com` → `im.qq.com` → `qfile.qq.com` → 无 Referer。
- `download_file()`、`download_image_by_url()` 全部改用上述头与重试逻辑，
  保留原有 SSL 降级兜底。

### 2. `astrbot/core/message/components.py`

- `Image` 新增 `_image_candidates()`：按「本地 `path` → 本地 `file` → `url`」排序。
- `convert_to_file_path()` / `convert_to_base64()` 逐个来源尝试，某个来源失败
  自动换下一个，避免过期 URL 直接让整条消息失败。

### 3. `astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py`

- 新增 `_resolve_qq_private_image()`：图片段 URL 命中
  `gchat.qpic.cn/download` 时，先调用协议端
  `get_image` / `get_file`（依次尝试 `file` / `file_id` / `id` 参数），
  拿到 base64 就落盘、拿到公开 URL 就替换，避免直接请求需要登录态的链接。
- 图片消息解析后自动应用，失败时静默回退到原 URL（不改变原行为）。

## 四、验证

本地（/workspace/AstrBot，v4.29.4 源码）：

- `normalize_download_url`：`&amp;`、引号、换行 → 正确 URL；
- 本地模拟 CDN：无 UA → 400，加 UA → 200；
- 模拟 Referer 校验：首个 Referer 400，第二个 200，重试成功；
- `MediaResolver` / `File.get_file` / `Image.convert_to_file_path` 均走新逻辑成功；
- 图片本地优先：坏 URL + 本地文件 → 直接返回本地文件；仅坏 URL → 明确抛错；
- 适配器 `_resolve_qq_private_image`：base64 → 临时 jpg；返回公开 URL → 替换；
- 非私有 CDN 图片不触发额外 API 调用。

线上（面板热修复插件 v1.0.3）：

```
GET /api/v1/plugins/extensions/astrbot_plugin_qq_download_fix/test?url=...
{"status":"ok","http":200,"bytes":77007,"patched":true,"adapter_patched":true}
```

## 五、部署方式

### 方式 A：核心补丁（推荐，长期）

```bash
cd <AstrBot 目录>
git apply --check /workspace/qq-download-fix.patch   # 先检查
git apply /workspace/qq-download-fix.patch
# 重启 AstrBot
```

补丁基于 v4.29.4（commit 630d5ce）生成，涉及 3 个文件、+349/-95 行。

### 方式 B：热修复插件（已安装到线上）

`astrbot_plugin_qq_download_fix` v1.0.3，面板可直接卸载。
它在运行期把同样的逻辑打到内存里，核心升级后可移除。

## 六、文件

| 文件 | 说明 |
|---|---|
| `/workspace/AstrBot`（3 个文件已修改） | 核心源码修复 |
| `/workspace/qq-download-fix.patch` | 核心补丁（git apply） |
| `/workspace/astrbot_plugin_qq_download_fix/` | 热修复插件源码 |
| `/workspace/astrbot_plugin_qq_download_fix-v1.0.3.zip` | 热修复插件安装包 |
