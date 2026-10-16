# 部署成远程服务，给网页版 claude.ai 用

部署完以后，在 claude.ai 里添加一个自定义 connector，网页版和手机端的对话里就都能直接生图了。

## 准备

- 一台**海外**服务器（OpenAI 不对中国大陆提供服务），装好 Docker 和 Docker Compose
- 一个域名，解析到这台服务器（claude.ai 要求 HTTPS）

## 1. 拉代码，填配置

```bash
git clone https://github.com/hui455/openai-image-gen-community.git
cd openai-image-gen-community
cp .env.example .env
openssl rand -hex 24      # 生成密钥，复制输出
nano .env                 # 填 OPENAI_API_KEY、MCP_SECRET、PUBLIC_BASE_URL、DOMAIN
```

用中转站的话，把 `OPENAI_BASE_URL` 前面的 `#` 去掉，再填上地址。

## 2. 启动，二选一

**A. 服务器上还没有 Nginx 之类的反向代理**：用自带的 Caddy，会自动申请 HTTPS 证书

```bash
docker compose --profile caddy up -d --build
```

要求 80 和 443 端口空着，域名已经解析到这台服务器。

**B. 已经有 Nginx 或其他反向代理**：只启动服务本身，它会监听 `127.0.0.1:8000`

```bash
docker compose up -d --build
```

然后在 Nginx 里把你的域名反代到 `http://127.0.0.1:8000`。关掉缓冲，否则流式响应会卡住：

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_buffering off;
    proxy_read_timeout 300s;
}
```

## 3. 检查

```bash
scripts/verify.sh https://你的域名 你的MCP_SECRET     # 全部 PASS 才算部署成功
docker compose logs -f image-gen                     # 看日志
```

检查脚本会测公网能否访问、是不是 HTTPS、没带密钥的请求是否被拒、MCP 握手、工具列表、服务端有没有读到 API Key。全部通过时，它会把要填进 claude.ai 的地址打印出来。

> 让 agent 帮你部署的话，把仓库里的 `AGENTS.md` 交给它，里面是给 agent 写的逐步操作和排错表。

## 4. 在 claude.ai 里添加

打开 claude.ai 的 connector 设置，添加自定义 connector：

- 名称：随便取，比如 `生图`
- URL：`https://你的域名/你的MCP_SECRET/mcp`
- 鉴权方式不用填

添加后在对话里打开这个 connector，问一句"现在用的是哪个生图模型"试试。

## 安全

- **MCP_SECRET 就是密码**。拿到完整 URL 的人都能花你的额度，不要发给别人，也不要截图外传
- 泄露了就换一个新的 `MCP_SECRET`，然后执行 `docker compose up -d`，再到 claude.ai 里更新 URL
- 图片下载链接使用每张图片独立的随机令牌，不包含 MCP_SECRET。拿到链接的人只能下载对应图片，不能用它调用生图工具；链接仍应仅分享给你希望看到图片的人。
- 下载令牌保存在图片卷的 `.download-tokens` 目录，重启和更新后保持有效；不要删除该目录，除非要撤销所有图片链接。旧的 `/<MCP_SECRET>/files/...` 路由已停用，旧图片可通过 `list_images` 获取新链接。若旧链接曾对外分享，其包含的连接密钥仍应另行轮换。
- 最好在 OpenAI 后台给这个 Key 设置一个消费上限

## 更新

```bash
git pull && docker compose up -d --build     # 用 Caddy 的话加上 --profile caddy
```

## 存储

生成的图片存在 Docker 卷 `images` 里，重启和更新都不会丢。要清理的话：

```bash
docker compose exec image-gen sh -c 'rm -f /data/images/*'
```

## 网页图片管理

在 `.env` 设置独立的 `IMAGE_ADMIN_PASSWORD`（至少 16 个字符，勿复用 MCP_SECRET 或 API Key），重启容器后访问 `https://你的域名/admin`。可预览、搜索、排序、下载、复制分享链接及删除选中图片。登录时默认勾选“记住我”：30 天内不用再输密码，服务器重启或更新也不会掉线；不勾选则关闭浏览器即退出（最长 8 小时）。浏览器自带的密码管理器（如 Chrome、Google 密码管理器）可以保存这个密码。会话只以哈希形式存在图片卷的 `.admin-sessions.json`，修改 `IMAGE_ADMIN_PASSWORD` 后所有设备都会被要求重新登录。删除无法撤销且对应分享链接失效，请先下载需要保留的原图。未配置密码时管理接口禁止登录；连续 5 次密码错误后该 IP 暂停登录 15 分钟。

## 磁盘保护

默认图片总量上限为 1 GiB（IMAGE_STORAGE_MAX_BYTES=1073741824），保留至少 2 GiB 可用空间（IMAGE_MIN_FREE_BYTES=2147483648）。生图和编辑调用前、图片保存前都会检查，达到限制会报错，不自动删除旧图。单进程内同时只允许一个生图或编辑请求，其他请求提示稍后重试。API 调用期间磁盘空间如果被其他服务占用，保存可能被拒绝，但已产生的 API 费用无法撤销。此保护是应用级限制，不是整个服务器或 Docker 卷的文件系统配额。

## 模型与默认配置

默认使用 `gpt-image-2.5-flare`，也可以用 `gpt-image-2.5-sunburst`（更精细、更慢）。中转站提供的其他模型名同样可以填在 `.env` 的 `IMAGE_MODEL` 里，会原样传给接口；参数按 gpt-image 系列的格式发送。

MCP 的 `set_image_config` 工具可以修改默认的 model、size、quality，写入图片卷里的 `.image-config.json`，优先于环境变量，更新或重启都不会丢。这份配置对所有连接到这个服务的 MCP 客户端共享；单次生成或编辑时传入的参数只影响那一次，不会改掉默认值。

## 提示词和参考图记录

每次生成或编辑，都会把提示词、模型改写后的提示词（有的话）、模型、尺寸、画质、背景和用到的参考图 / 遮罩记下来，存在图片卷的隐藏目录 `.image-meta`（每张图一个 JSON 文件，不能被下载或列出）。

- **参考图**：图库里已有的图只记文件名，不另存；本服务的下载链接也按图库里的图处理。网上的图片链接和本地路径会另存一份副本到图库，命名为 `<新图名>-ref1.png`（遮罩为 `-mask`），以免原链接失效后找不到。
- **图库后台**：预览信息栏里显示提示词（可一键复制）、参数和参考图缩略图，点参考图能跳过去看，再点“返回”回来。搜索框同时搜文件名和提示词。删除一张被别的图用作参考的图片时，删除弹窗会提醒；删掉后那些图的记录里显示“参考图已删除”，提示词照样保留。删除图片时它自己的记录一起删除。
- **Claude 这边**：`list_images` 显示每张图的提示词摘要和参考图，`image_info` 返回完整提示词和参数，可以用来“照着那张再画一张”。
- 这个功能上线之前生成的图没有记录，信息栏里会注明。记录写入失败不会影响生图本身。

## 对话里显示图片

每次生成或编辑完，回复末尾的 ps 会把**原图**直接显示在对话里（Markdown 图片，地址就是下载链接），清晰但每张要加载几 MB。原图链接按“内嵌”方式返回，浏览器里打开是直接看图；图库后台的下载按钮照常保存文件。图片会在设备上缓存一天，来回滑动对话不会重复下载。

`/thumbs/<令牌>/<文件名>` 缩略图链接仍然保留（和下载链接同一个令牌，最长边由 `IMAGE_PREVIEW_MAX_PX` 决定），旧对话里的缩略图不会失效。`IMAGE_PREVIEW_MAX_PX` 同时决定返回给 Claude 的预览图大小。
