# 图片管理网页审阅

远程服务部署后访问 `/admin`。设置独立的 `IMAGE_ADMIN_PASSWORD`（至少 16 个字符），不复用 MCP 连接密钥。源码和图片示例不包含真实密码、API Key 或服务器图片。

本仓库不包含私人图库截图或生产环境数据。README 中的页面截图来自实际运行的本地系统，使用专门制作的演示素材。

## 功能与范围

- 独立密码登录，支持退出。“记住我”（默认勾选）保持登录 30 天，服务器重启后依然有效；不勾选则关闭浏览器即退出（最长 8 小时）。浏览器密码管理器可保存密码。修改管理密码会让所有设备重新登录。登录页不显示任何图片。
- 顶栏一行显示图片存储用量、上限和服务器剩余空间；触发容量保护时进度条变红并给出提示。
- 文件名和提示词搜索；排序：最新、最早、最大；每页 12 / 24 / 48 / 96 张（默认 24，浏览器会记住选择）；底部显示全部页码（页数多时用“…”省略），可直接跳页。
- 固定 3:4 网格，图片居中裁切铺满；列数随窗口宽度自动变化（桌面 5 列，窄屏 4 / 3 列，手机 2 列）。透明图片显示棋盘格底。
- 鼠标悬停时显示文件名、大小和“复制分享链接 / 下载原图”按钮；手机上点图片直接打开预览。
- 大图预览：完整原图 + 尺寸、大小、格式、生成时间；下载、复制链接、删除这张；左右按钮或键盘 ← → 切换（可跨页），Esc 关闭。
- 选择模式：底部浮出操作栏（已选数量、全选、批量下载、取消、删除）；删除确认弹窗显示要删的缩略图；Esc 退出选择。
- 生图进行中仍可浏览图库（顶部提示“正在生成图片”）；删除需等生图结束。
- 不提供 API Key 编辑、生图或自动清理功能。不会展示 MCP 连接密钥。

登录接口限制密码尝试频率，只信任反向代理（私有网段，可用 `TRUSTED_PROXY_IPS` 调整）传来的客户端 IP，伪造 `X-Forwarded-For` 无法绕过。管理会话使用 Secure、HttpOnly、SameSite=Strict Cookie；写入操作检查请求来源和 CSRF 令牌。删除限制在图片目录内，拒绝符号链接和路径穿越，正在生图时返回稍后重试。下载令牌仅能读取对应图片，不能访问管理接口或 MCP。

## 验证

测试覆盖认证、图片下载、容量限制、图库、配置、缩略图、持久登录以及提示词和参考图记录。测试使用临时图片和模拟 API。

服务器上可用以下命令复测，测试只使用临时目录和模拟 API，不生成付费图片：

```bash
docker compose build
docker compose run --rm --no-deps -T image-gen python - < tests/test_storage.py
docker compose run --rm --no-deps -T image-gen python - < tests/test_downloads.py
docker compose run --rm --no-deps -T image-gen python - < tests/test_admin.py
docker compose run --rm --no-deps -T image-gen python - < tests/test_fixes.py
docker compose run --rm --no-deps -T image-gen python - < tests/test_gallery.py
docker compose run --rm --no-deps -T image-gen python - < tests/test_config.py
docker compose run --rm --no-deps -T image-gen python - < tests/test_thumbs.py
docker compose run --rm --no-deps -T image-gen python - < tests/test_login.py
docker compose run --rm --no-deps -T image-gen python - < tests/test_records.py
```

页面代码：`plugins/openai-image-gen/admin_assets/`；权限与管理接口：`plugins/openai-image-gen/admin.py`。
