# AstrBot 4.29.5冰霜特更版 · QQ 图片 / 文件获取修复

基于 AstrBot v4.29.4（commit `630d5ce`）。这是一个定向修复版本，不包含其他功能改动。

## 问题

QQ 群消息里的图片、群文件由协议端（NapCat / LLOneBot）给出形如
`https://gchat.qpic.cn/download?appid=...&fileid=...&rkey=...` 的链接。
AstrBot 4.29.4 直接 `aiohttp` GET 这个链接，会出现：

```
HTTP 400 {"retcode":-5503007,"retmsg":"download url has expired"}
[WARN] [preprocess_stage.stage:184]: Image processing failed ...
```

原因有两层：
1. 这类链接依赖 QQ 登录态（rkey），且 **rkey 很快过期**；
2. 下载请求没有浏览器 UA / Referer，部分 CDN 直接拒绝。

结果：图片进不了 LLM，群文件也无法保存。

## 修复

### 1. 优先使用协议端本地缓存（关键）

`astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py`

- 图片段 URL 命中 `gchat.qpic.cn/download` 时，改调 OneBot 的
  `get_image` / `get_file`（依次尝试 `file` / `file_id` / `id`）。
- 协议端返回的 `file` 如果是**本地路径**（NapCat 会返回
  `.../nt_data/Pic/.../xxx.jpg`），直接使用该文件，**不再请求过期 URL**。
- 返回 `base64://` 时落盘为临时文件；只有本地路径不可用时才回退到 URL。
- 群文件同理：新增 `_try_qq_file_local()`，优先 `get_file` 本地缓存，
  失败才用 URL 创建 `File` 组件。

### 2. 下载层：浏览器请求头 + 重试 + URL 清洗

`astrbot/core/utils/io.py`

- `normalize_download_url()`：HTML 反转义（`&amp;`）、去引号/零宽字符/换行。
- `build_download_headers()`：浏览器 UA、`Accept`、`Accept-Language`。
- 腾讯系域名自动带 `Referer`；遇到 400/403 依次尝试
  `qun.qq.com` → `im.qq.com` → `qfile.qq.com` → 无 Referer。
- `download_file()` / `download_image_by_url()` 全部接入上述逻辑，
  并保留 SSL 校验失败时的降级重试。

### 3. 图片组件本地来源优先

`astrbot/core/message/components.py`

- `Image._image_candidates()`：按「本地 `path` → 本地 `file` → `url`」排序。
- `convert_to_file_path()` / `convert_to_base64()` 逐个来源尝试，
  本地缓存可用时完全不走网络。

## 版本

- `pyproject.toml`: `4.29.4` → `4.29.5`
- `astrbot/__init__.py`: `__version__ = "4.29.5"`，新增 `__display_version__ = "4.29.5冰霜特更版"`
- 面板/启动横幅显示「4.29.5冰霜特更版」（`VERSION_DISPLAY`），内部版本号仍为可比较的 `4.29.5`

## 验证记录

线上日志（修复后）：

```
13:03:30  命中 QQ 私有图片 file='A415...jpg'
13:03:32  get_image → /root/napcat_instances/.../nt_data/Pic/2026-11/Ori/a415....jpg  exists=True
13:03:32  已使用协议端本地缓存: .../a415....jpg
13:03:32  [图片] 正常进入事件总线（无 Image processing failed）
```

修复前同一路径返回：

```
HTTP 400 {"retcode":-5503007,"retmsg":"download url has expired"}
```
