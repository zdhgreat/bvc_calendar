# bbs-go 脚本自动化接口文档

本文档面向站点维护者编写自动化脚本，内容依据当前仓库中的路由、请求结构、
处理器和前端实际调用整理。示例中的 `https://bbs.example.com`、分类 ID、帖子 ID
和 token 均需替换为目标站点的实际值。

> 自动化程序应遵守站点规则。验证码、邮箱验证、观察期和禁言等限制属于服务端安全策略，不应绕过。

## 1. 接口概览

| 功能 | 方法 | 路径 | 登录 |
| --- | --- | --- | --- |
| 获取公开配置 | `GET` | `/api/config/configs` | 否 |
| 获取验证码 | `GET` | `/api/captcha/request` | 否 |
| 密码登录 | `POST` | `/api/login/signin` | 否 |
| 验证当前 token | `GET` | `/api/user/current` | 可选 |
| 获取发帖分类 | `GET` | `/api/topic/categories` | 按站点配置 |
| 创建帖子 | `POST` | `/api/topic/create` | 是 |
| 获取帖子详情 | `GET` | `/api/topic/{topicId}` | 按站点配置 |
| 获取帖子列表 | `GET` | `/api/topic/topics` | 按站点配置 |
| 获取/提交帖子编辑数据 | `GET` / `POST` | `/api/topic/edit/{topicId}` | 是 |
| 删除帖子 | `POST` | `/api/topic/delete/{topicId}` | 是 |
| 上传正文或评论图片 | `POST` | `/api/upload` | 是 |
| 获取一级评论 | `GET` | `/api/comment/comments` | 按站点配置 |
| 获取二级回复 | `GET` | `/api/comment/replies` | 按站点配置 |
| 创建评论或回复 | `POST` | `/api/comment/create` | 是 |
| 删除评论或回复 | `POST` | `/api/comment/delete/{commentId}` | 是 |
| 退出并注销 token | `GET` | `/api/login/signout` | 可选 |

基础地址约定：

```bash
export BBS_BASE_URL='https://bbs.example.com'
# BBS_TOKEN 由第 4 节的登录流程或运行环境中的密钥管理服务提供。
```

本文的命令行示例使用 `curl`；第 4 节的一次性登录流程还需要 `jq` 和 GNU `base64`。Python 示例要求 Python 3.10 或更高版本。

路径均相对于 `BBS_BASE_URL`，例如：

```text
https://bbs.example.com/api/topic/create
```

## 2. 通用协议

### 2.1 认证

脚本推荐只发送 `X-User-Token`：

```http
X-User-Token: <token>
```

也支持标准 Bearer 写法。`Bearer` 的大小写必须保持如下，并在其后保留一个空格：

```http
Authorization: Bearer <token>
```

服务端查找凭据的优先级是：

1. 查询参数或表单字段 `userToken`
2. Cookie `bbsgo_token`
3. `Authorization` Header
4. `X-User-Token` Header

不要在 URL 中传 `userToken`，否则 token 容易进入访问日志。也不要混用多种认证方式：
失效的高优先级 Cookie 或 Header 会覆盖后面的有效 token。使用
`requests.Session` 等会保存 Cookie 的客户端时尤其需要注意。

token 是服务端持久化的不透明字符串，不是 JWT。有效期由完整配置中的
`tokenExpireDays` 决定；代码回退值是 7 天，新安装站点的初始配置通常是 365 天，
应以目标站点返回值为准。

当前配置还支持 `loginRequired`。启用时，除了安装、登录、验证码、邮箱验证、
公开配置和当前用户等白名单接口，其他 `/api` 请求即使原本支持匿名读取，
也必须携带有效 token。脚本取得 token 后，最稳妥的做法是给后续所有 API 请求
统一添加认证 Header。

### 2.2 统一响应

普通成功响应：

```json
{
  "errorCode": 0,
  "message": "",
  "data": {},
  "success": true
}
```

普通失败响应：

```json
{
  "errorCode": 1000,
  "message": "验证码错误",
  "data": null,
  "success": false
}
```

业务成功和业务失败通常都会返回 HTTP `200`。脚本必须先检查 HTTP 状态，再检查 JSON 中的 `success`。不能只检查 `errorCode`，因为普通参数错误可能是：

```json
{
  "errorCode": 0,
  "message": "参数错误说明",
  "data": null,
  "success": false
}
```

### 2.3 ID 类型

| 对象 | API 中的常见类型 | 使用规则 |
| --- | --- | --- |
| 帖子 ID | 不透明字符串 | 原样使用创建、列表或详情响应中的 `topic.id` |
| 用户 ID | 不透明字符串 | 原样使用响应值 |
| 分类 ID | 正整数 | 使用分类接口返回的 `category.id` |
| 评论/回复 ID | 正整数 | 使用评论响应中的 `comment.id` |
| 附件 ID | UUID 字符串 | 使用附件上传响应中的 `id` |

帖子 ID 不应由脚本自行编码或解码。评论响应中的 `entityId` 是服务端内部整数，即使该评论属于帖子，也不要用它替代原始的外部帖子 ID。

### 2.4 游标分页与时间

列表接口统一在 `data` 中返回：

```json
{
  "results": [],
  "cursor": "下一页游标",
  "hasMore": false
}
```

当 `hasMore` 为 `true` 时，把 `cursor` 原样放到下一次同接口请求中。游标虽然经常只包含数字，但其含义会随接口和排序方式变化，脚本应将它视为不透明字符串。

空列表在部分处理路径中会序列化为 `results:null`，而不是 `results:[]`。
客户端应使用 `results = data.get("results") or []` 一类逻辑把两者统一为空数组。

`createTime`、`lastCommentTime`、`expiredAt` 等时间字段使用 Unix 毫秒时间戳。

## 3. 调用前探测

### 3.1 获取公开配置

```http
GET /api/config/configs
```

```bash
# 登录前可匿名读取登录所需的最小配置
curl -sS "$BBS_BASE_URL/api/config/configs"

# 登录后携带 token 可读取完整公开配置
curl -sS \
  -H "X-User-Token: $BBS_TOKEN" \
  "$BBS_BASE_URL/api/config/configs"
```

当 `loginRequired=true` 且请求未登录时，只有站点标题、安全可公开的站点 Logo、
`loginRequired`、登录方式配置、`installed` 和 `language` 是有效的公开配置；
其他字段即使仍出现在 JSON 中，也会被清空或置为零值。不要把匿名响应中的
`topicCaptcha=false`、`tokenExpireDays=0` 等零值当成真实站点设置，取得 token 后
必须重新请求。

脚本至少应关注以下 `data` 字段：

| 字段 | 说明 |
| --- | --- |
| `installed` | 站点是否已经安装 |
| `loginRequired` | 是否强制登录后访问内容 API |
| `tokenExpireDays` | token 服务端有效天数 |
| `topicCaptcha` | 发帖是否需要验证码 |
| `createTopicEmailVerified` | 发帖是否要求已验证邮箱 |
| `createCommentEmailVerified` | 评论是否要求已验证邮箱 |
| `userObserveSeconds` | 新用户观察期秒数 |
| `modules.topic` | 普通帖子模块是否开启 |
| `modules.tweet` | 动态模块是否开启 |
| `modules.qa` | 问答模块是否开启 |
| `enableHideContent` | 回复后可见内容是否开启 |
| `enableQaBounty` | 问答悬赏是否开启 |
| `qaBountyMin` / `qaBountyMax` | 悬赏积分范围 |
| `qaBountyRequired` | 问答是否必须设置悬赏 |
| `attachmentConfig` | 附件开关、扩展名、大小和数量配置 |
| `loginConfig.passwordLogin.enabled` | 普通用户密码登录是否开启 |

站点未安装时，其他 API 会返回 `success:false,errorCode:-1`。

### 3.2 获取可用分类

```http
GET /api/topic/categories?type={topicType}
```

`type` 可省略；指定时取值为：

| 值 | 类型 |
| ---: | --- |
| `0` | 普通帖子 |
| `1` | 动态 |
| `2` | 问答 |

```bash
curl -sS \
  -H "X-User-Token: $BBS_TOKEN" \
  "$BBS_BASE_URL/api/topic/categories?type=0"
```

成功响应的 `data` 是分类树：

```json
[
  {
    "id": 1,
    "parentId": 0,
    "name": "技术交流",
    "type": "normal",
    "logo": "/res/images/category_default.svg",
    "description": ""
  }
]
```

创建帖子时必须显式传入目标站点返回的有效正整数 `categoryId`。当前实现中的
默认分类回退只修改了校验函数的局部副本，不能可靠地写入最终帖子，
因此不要省略或传 `0`。

### 3.3 写操作的账户前置条件

创建帖子、评论、图片以及多数编辑或删除操作会统一检查当前用户状态。调用者必须已经登录、账号状态正常、未被禁言且已经结束新用户观察期。站点配置开启相应开关时，发帖还要求邮箱已验证，评论也可单独要求邮箱已验证。

这些检查失败时通常返回第 9 节中的 `1`、`1001`、`1002`、`1003` 或 `1004`。失败后应停止当前发布任务，不要用高频重试规避限制。

## 4. 登录与 token

### 4.1 获取字符验证码

密码登录始终要求验证码。适合命令行人工完成一次登录的旧字符验证码接口为：

```http
GET /api/captcha/request
```

```json
{
  "errorCode": 0,
  "message": "",
  "data": {
    "captchaId": "验证码ID",
    "captchaBase64": "不含 data:image/png;base64, 前缀的PNG Base64"
  },
  "success": true
}
```

Linux 命令行示例：

```bash
export BBS_TMP_DIR="$(mktemp -d)"
chmod 700 "$BBS_TMP_DIR"
trap 'rm -rf -- "$BBS_TMP_DIR"' EXIT

curl -sS "$BBS_BASE_URL/api/captcha/request" > "$BBS_TMP_DIR/captcha.json"
jq -r '.data.captchaBase64' "$BBS_TMP_DIR/captcha.json" \
  | base64 --decode > "$BBS_TMP_DIR/captcha.png"
export BBS_CAPTCHA_ID="$(jq -r '.data.captchaId' "$BBS_TMP_DIR/captcha.json")"
```

打开 `$BBS_TMP_DIR/captcha.png`，人工读取其中的 4 个字符。登录时设置
`captchaProtocol=0`。临时目录权限为 `0700`，退出当前 Shell 时会自动清理。

站点前端当前使用旋转验证码：

```http
GET /api/captcha/request_angle
```

其 `data` 包含 `id`、`imageBase64`、`thumbBase64` 和 `thumbSize`，提交时使用
`captchaProtocol=2`，并将人工旋转得到的角度作为 `captchaCode`。
自动化程序不应伪造或绕过验证码。

### 4.2 密码登录

```http
POST /api/login/signin
Content-Type: application/x-www-form-urlencoded
```

| 字段 | 必填 | 说明 |
| --- | ---: | --- |
| `username` | 是 | 用户名或邮箱 |
| `password` | 是 | 密码 |
| `captchaId` | 是 | 验证码 ID |
| `captchaCode` | 是 | 字符验证码或旋转角度 |
| `captchaProtocol` | 是 | `2` 为旋转验证码，其他值走字符验证码 |
| `redirect` | 否 | 原样返回给客户端的跳转地址 |

```bash
export BBS_USERNAME='alice'
read -r -s -p '密码: ' BBS_PASSWORD
printf '\n'
read -r -p '验证码: ' BBS_CAPTCHA_CODE

printf '%s' "$BBS_PASSWORD" | curl -sS -X POST \
  "$BBS_BASE_URL/api/login/signin" \
  --data-urlencode "username=$BBS_USERNAME" \
  --data-urlencode 'password@-' \
  --data-urlencode "captchaId=$BBS_CAPTCHA_ID" \
  --data-urlencode "captchaCode=$BBS_CAPTCHA_CODE" \
  --data-urlencode 'captchaProtocol=0' > "$BBS_TMP_DIR/login.json"
unset BBS_PASSWORD BBS_CAPTCHA_CODE

jq '{success, errorCode, message, user: (.data.user // null)}' \
  "$BBS_TMP_DIR/login.json"
BBS_TOKEN="$(jq -r 'if .success then .data.token else empty end' \
  "$BBS_TMP_DIR/login.json")"
export BBS_TOKEN

rm -rf -- "$BBS_TMP_DIR"
trap - EXIT
unset BBS_TMP_DIR BBS_CAPTCHA_ID
```

密码通过标准输入交给 `curl`，不会出现在命令参数中；登录响应位于权限受限的临时目录，
且展示响应时会排除 token。继续操作前确认 `BBS_TOKEN` 非空。

成功响应的关键数据：

```json
{
  "success": true,
  "errorCode": 0,
  "message": "",
  "data": {
    "token": "32位不透明token",
    "redirect": "",
    "user": {
      "id": "编码后的用户ID",
      "username": "alice",
      "nickname": "Alice",
      "emailVerified": true
    }
  }
}
```

登录响应还会设置 HttpOnly Cookie `bbsgo_token`。脚本直接保存 `data.token` 即可，且必须像密码一样保管，不要提交到仓库或写入公开日志。

### 4.3 验证 token

```http
GET /api/user/current
X-User-Token: <token>
```

```bash
curl -sS \
  -H "X-User-Token: $BBS_TOKEN" \
  "$BBS_BASE_URL/api/user/current"
```

token 有效时 `data` 为当前用户资料。无 token、token 无效或 token 过期时，
该接口仍返回 `success:true`，但 `data` 为 `null`，因此应同时检查这两个条件。

### 4.4 注销 token

```http
GET /api/login/signout
X-User-Token: <token>
```

该接口会把数据库中的当前 token 标记为删除，并发送 Cookie 删除指令。即使请求中
没有有效 token，也可能返回 `success:true`，因此成功响应只表示注销处理没有报错，
不代表请求头中的 token 已即时失效。它使用 `GET` 而不是 `POST`，脚本应按现有路由
调用。

> **当前实现风险：** 当前版本没有同步失效进程内的 `UserTokenCache`。已经进入缓存
> 的 `X-User-Token` 在注销成功后仍可能继续通过认证；缓存按访问续期，不能把残留
> 有效期简单理解为固定 60 分钟。脚本必须立即从本地存储和后续请求头中删除 token，
> 不要继续调用 `/api/user/current` 轮询失效，因为访问本身会延长缓存存活时间。需要
> 即时、可靠的服务端撤销时，当前版本的注销接口无法提供该保证。

## 5. 帖子接口

### 5.1 创建帖子

```http
POST /api/topic/create
Content-Type: application/json
X-User-Token: <token>
```

这是 JSON 专用接口，不要用表单提交。

最小可靠请求：

```bash
curl -sS -X POST "$BBS_BASE_URL/api/topic/create" \
  -H "X-User-Token: $BBS_TOKEN" \
  -H 'Content-Type: application/json' \
  --data-binary @- <<'JSON'
{
  "type": 0,
  "categoryId": 1,
  "title": "脚本发布的帖子",
  "contentType": "markdown",
  "content": "## 正文\n\n这是 **Markdown** 内容。",
  "tags": ["自动化"]
}
JSON
```

字段说明：

| 字段 | 必填 | 类型 | 规则 |
| --- | ---: | --- | --- |
| `type` | 是 | number | `0` 普通帖子、`1` 动态、`2` 问答 |
| `categoryId` | 是 | number | 有效正整数，且分类类型必须支持当前帖子类型 |
| `title` | 条件 | string | 普通帖子和问答必填，最多 128 个 Unicode 字符；动态可为空 |
| `content` | 是 | string | 所有类型均必填；服务端会去除首尾空白 |
| `contentType` | 是 | string | 推荐 `markdown`；还支持 `html`、`text`；动态强制为 `text` |
| `tags` | 否 | string[] | 标签名数组；不要发送空字符串标签 |
| `hideContent` | 否 | string | 回复后可见内容，仅在站点开启该功能时使用；问答会忽略并清空 |
| `imageList` | 否 | object[] | `[{"url":"..."}]`，主要用于动态图片 |
| `vote` | 否 | object/null | 投票配置；问答会忽略 |
| `bountyScore` | 否 | number | 仅在问答悬赏开启时使用；关闭时必须省略或传 `0` |
| `attachmentIds` | 否 | string[] | 普通帖子附件 UUID；必须先由附件接口上传 |
| `captchaId` | 条件 | string | `topicCaptcha=true` 时必填 |
| `captchaCode` | 条件 | string | `topicCaptcha=true` 时必填 |
| `captchaProtocol` | 条件 | number | `0` 为字符验证码，`2` 为旋转验证码 |

不要发送 `ip` 或 `userAgent`，处理器会用实际请求信息覆盖它们。

> **悬赏关闭时的当前实现风险：** 参数校验对 `bountyScore` 的清零只作用于局部副本，
> 后续发布仍可能使用调用方传入的正值并扣除积分。因此当 `enableQaBounty=false` 时，
> 脚本必须省略该字段或显式传 `0`，不能依赖服务端替调用方清零。

`vote` 对象结构：

```json
{
  "type": 1,
  "title": "选择一个选项",
  "expiredAt": 1893456000000,
  "voteNum": 1,
  "options": [
    {"content": "选项 A"},
    {"content": "选项 B"}
  ]
}
```

投票规则：`type=1` 为单选，`type=2` 为多选；标题最多 128 字；选项数为 2 至 20，
每项最多 256 字；`expiredAt` 必须是未来的毫秒时间戳；多选时 `voteNum` 必须在
`1..选项数` 内。

开启发帖验证码时，可复用第 4.1 节的验证码请求流程，并把验证码字段放进创建帖子的 JSON。验证码挑战应在提交前即时获取。

成功响应中的 `data` 是帖子摘要，关键字段如下：

```json
{
  "success": true,
  "errorCode": 0,
  "message": "",
  "data": {
    "id": "不透明帖子ID",
    "type": 0,
    "title": "脚本发布的帖子",
    "summary": "正文摘要",
    "content": "",
    "status": 0,
    "createTime": 1780000000000
  }
}
```

普通帖子创建响应不返回原始正文，只返回摘要；后续操作应保存 `data.id`。
`status=0` 表示正常，内容命中违禁词时接口仍可能创建成功，但 `status=2`
表示待审核。

### 5.2 获取帖子详情

```http
GET /api/topic/{topicId}
```

```bash
curl -sS \
  -H "X-User-Token: $BBS_TOKEN" \
  "$BBS_BASE_URL/api/topic/$TOPIC_ID"
```

对 Markdown 帖子，详情响应中的 `data.content` 是服务端转换后的 HTML，不是创建时的原始 Markdown。该接口每调用一次都会增加浏览量，不要把它作为高频轮询接口。

待审核帖子仅作者本人或站长可查看；此时请求需要携带 token。

### 5.3 获取帖子列表

```http
GET /api/topic/topics
```

| Query 参数 | 必填 | 说明 |
| --- | ---: | --- |
| `categoryId` | 否 | `0` 全部、`-1` 推荐、`-2` 关注流、正数为分类 |
| `cursor` | 否 | 上一页返回的游标 |
| `qaStatus` | 否 | `unsolved` 或 `solved`；设置后只查问答 |
| `sort` | 否 | `latestPublish` 或 `latestReply`；默认按最新回复 |

```bash
curl -sS \
  -H "X-User-Token: $BBS_TOKEN" \
  --get "$BBS_BASE_URL/api/topic/topics" \
  --data-urlencode 'categoryId=1' \
  --data-urlencode 'sort=latestPublish'
```

`categoryId=-2` 的关注流必须登录。普通分页每页最多 30 条，但第一页可能额外包含置顶帖子，脚本不应依赖固定结果数量。

### 5.4 编辑帖子

先读取原始编辑数据：

```http
GET /api/topic/edit/{topicId}
X-User-Token: <token>
```

该响应包含原始 `content`、`contentType`、`categoryId`、`title`、`hideContent`、`tags` 和附件信息。动态不支持这个编辑表单接口。

提交编辑：

```http
POST /api/topic/edit/{topicId}
Content-Type: application/json
X-User-Token: <token>
```

```json
{
  "categoryId": 1,
  "title": "更新后的标题",
  "content": "更新后的正文",
  "hideContent": "",
  "tags": ["自动化", "更新"],
  "attachmentIds": []
}
```

这是全量更新语义，不是 JSON Merge Patch：应先读取编辑数据、修改目标字段，
再提交完整内容。省略 `tags` 会清空标签；`attachmentIds` 省略时保持原附件，
显式传 `[]` 时清空附件。接口不支持修改 `contentType`。

只有帖子作者或站长可编辑。标题必填且最多 128 字，分类必须存在并支持原帖子类型。

### 5.5 删除帖子

```http
POST /api/topic/delete/{topicId}
X-User-Token: <token>
```

```bash
curl -sS -X POST \
  -H "X-User-Token: $BBS_TOKEN" \
  "$BBS_BASE_URL/api/topic/delete/$TOPIC_ID"
```

只有帖子作者或站长可删除。删除为软删除；目标已不存在或已删除时仍返回成功。

## 6. 评论与回复

### 6.1 创建一级评论

```http
POST /api/comment/create
Content-Type: application/x-www-form-urlencoded
X-User-Token: <token>
```

评论接口按现有前端调用使用表单，不要把 `imageList` 直接作为 JSON 数组提交。

| 字段 | 必填 | 说明 |
| --- | ---: | --- |
| `entityType` | 是 | 帖子评论为 `topic`，文章评论为 `article`，二级回复为 `comment` |
| `entityId` | 是 | 一级帖子评论传外部帖子 ID；二级回复传一级评论 ID |
| `content` | 是 | 纯文本，服务端去除首尾空白并在响应时 HTML 转义 |
| `imageList` | 否 | JSON 字符串，例如 `[{"url":"https://..."}]` |
| `quoteId` | 否 | 回复某条二级回复时传被引用的评论 ID |

```bash
curl -sS -X POST "$BBS_BASE_URL/api/comment/create" \
  -H "X-User-Token: $BBS_TOKEN" \
  --data-urlencode 'entityType=topic' \
  --data-urlencode "entityId=$TOPIC_ID" \
  --data-urlencode 'content=这是脚本发布的评论'
```

成功响应关键字段：

```json
{
  "success": true,
  "errorCode": 0,
  "message": "",
  "data": {
    "id": 123,
    "entityType": "topic",
    "entityId": 456,
    "contentType": "text",
    "content": "这是脚本发布的评论",
    "imageList": [],
    "commentCount": 0,
    "quoteId": 0,
    "createTime": 1780000000000
  }
}
```

保存 `data.id`，它是后续回复、查询回复和删除所需的十进制评论 ID。

当前服务只校验 `entityType` 非空和 `entityId` 可解码为正数，并未完整校验目标
是否真实存在。脚本应只使用查询接口返回的真实对象 ID，并将 `entityType`
限制为上述预期值，避免产生无法关联的评论数据。

### 6.2 回复一级评论

把一级评论 ID 作为 `entityId`，并把 `entityType` 设为 `comment`：

```bash
curl -sS -X POST "$BBS_BASE_URL/api/comment/create" \
  -H "X-User-Token: $BBS_TOKEN" \
  --data-urlencode 'entityType=comment' \
  --data-urlencode "entityId=$PARENT_COMMENT_ID" \
  --data-urlencode 'content=这是对一级评论的回复'
```

回复同一楼层中的某条二级回复时，`entityId` 仍然是一级评论 ID，并额外传 `quoteId`：

```bash
curl -sS -X POST "$BBS_BASE_URL/api/comment/create" \
  -H "X-User-Token: $BBS_TOKEN" \
  --data-urlencode 'entityType=comment' \
  --data-urlencode "entityId=$PARENT_COMMENT_ID" \
  --data-urlencode "quoteId=$QUOTED_REPLY_ID" \
  --data-urlencode 'content=这是引用回复'
```

### 6.3 获取一级评论

```http
GET /api/comment/comments?entityType=topic&entityId={topicId}&cursor={cursor}
```

```bash
curl -sS --get "$BBS_BASE_URL/api/comment/comments" \
  -H "X-User-Token: $BBS_TOKEN" \
  --data-urlencode 'entityType=topic' \
  --data-urlencode "entityId=$TOPIC_ID"
```

每页最多 20 条，按评论 ID 倒序。问答帖已采纳的答案会在第一页置顶，并占用其中一个位置。每条一级评论最多内嵌最早的 3 条二级回复；完整回复应调用下一节接口。

### 6.4 获取二级回复

```http
GET /api/comment/replies?commentId={parentCommentId}&cursor={cursor}
```

```bash
curl -sS --get "$BBS_BASE_URL/api/comment/replies" \
  -H "X-User-Token: $BBS_TOKEN" \
  --data-urlencode "commentId=$PARENT_COMMENT_ID"
```

每页最多 10 条，按回复 ID 正序。继续翻页时使用响应中的 `data.cursor`。

### 6.5 删除评论或回复

```http
POST /api/comment/delete/{commentId}
X-User-Token: <token>
```

```bash
curl -sS -X POST \
  -H "X-User-Token: $BBS_TOKEN" \
  "$BBS_BASE_URL/api/comment/delete/$COMMENT_ID"
```

评论作者可删除自己的评论；具有 `dashboard.comment.delete` 权限的管理用户也可删除。仅仅是所在帖子的作者，并不能删除其他用户的评论。删除为软删除，不会级联删除其二级回复。当前删除逻辑也不会同步减少帖子、父评论或用户的累计评论数，脚本不应根据删除响应自行假定计数已经变化。

### 6.6 采纳问答答案

问答帖作者或站长可采纳该帖下的一条一级评论：

```http
POST /api/topic/accept_answer/{topicId}
Content-Type: application/x-www-form-urlencoded
X-User-Token: <token>
```

```bash
curl -sS -X POST "$BBS_BASE_URL/api/topic/accept_answer/$TOPIC_ID" \
  -H "X-User-Token: $BBS_TOKEN" \
  --data-urlencode "commentId=$COMMENT_ID"
```

取消采纳：

```http
POST /api/topic/unaccept_answer/{topicId}
X-User-Token: <token>
```

> **这两个接口当前不是可安全重放的状态操作。** 重复调用采纳接口会再次向答案作者
> 发放悬赏积分；取消采纳不会追回已经发放的积分，取消后重新采纳也会再次发放。
> 自动化脚本必须先检查帖子的 `acceptedCommentId` 和 `qaStatus`，只提交一次，
> 且绝不能对采纳或取消采纳请求做自动重试。存在悬赏时，取消采纳不具备积分回滚语义。

## 7. 图片与附件

### 7.1 上传正文或评论图片

```http
POST /api/upload
Content-Type: multipart/form-data
X-User-Token: <token>
```

文件字段名必须是 `image`：

```bash
curl -sS -X POST "$BBS_BASE_URL/api/upload" \
  -H "X-User-Token: $BBS_TOKEN" \
  -F 'image=@/path/to/image.png'
```

成功数据：

```json
{
  "success": true,
  "errorCode": 0,
  "message": "",
  "data": {
    "url": "https://cdn.example.com/path/to/image.png"
  }
}
```

普通 Markdown 帖子可把 URL 写入正文：

```markdown
![说明](https://cdn.example.com/path/to/image.png)
```

动态或评论图片使用 `imageList`。评论接口中的 `imageList` 是 JSON 字符串，
而帖子创建接口中的 `imageList` 是真正的 JSON 数组，这是两个接口的重要差异。

当 `loginRequired=true` 且上传使用本地存储时，`/res/uploads/...` 下的图片资源
也要求有效登录。脚本直接下载这类 URL 时需要携带 token；浏览器页面通常通过
登录 Cookie 访问。外部对象存储或 CDN URL 是否需要额外鉴权由对应存储配置决定。

### 7.2 上传帖子附件

仅普通帖子支持附件，并且公开配置中的 `attachmentConfig.enabled` 必须为 `true`。

```http
POST /api/attachment/upload
Content-Type: multipart/form-data
X-User-Token: <token>
```

```bash
curl -sS -X POST "$BBS_BASE_URL/api/attachment/upload" \
  -H "X-User-Token: $BBS_TOKEN" \
  -F 'file=@/path/to/document.pdf' \
  -F 'downloadScore=0'
```

成功响应的 `data.id` 是附件 UUID。将一个或多个 UUID 放入创建帖子的 `attachmentIds` 数组后，服务端才会把附件绑定到该帖子。附件必须属于当前用户、扩展名符合站点配置、未绑定其他帖子，且总数不能超过配置限制。

## 8. Python 完整示例

安装依赖：

```bash
python3 -m pip install requests
```

脚本从环境变量读取已经人工登录取得的 token，不把账号密码或验证码写入源代码：

```python
import json
import os
from typing import Any

import requests


class BBSGoError(RuntimeError):
    pass


class BBSGoClient:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"X-User-Token": token})

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.session.request(
            method,
            f"{self.base_url}{path}",
            timeout=(5, 30),
            **kwargs,
        )
        response.raise_for_status()
        try:
            envelope = response.json()
        except ValueError as exc:
            raise BBSGoError("服务器未返回 JSON") from exc

        if envelope.get("success") is not True:
            code = envelope.get("errorCode")
            message = envelope.get("message") or "未知业务错误"
            raise BBSGoError(f"API error {code}: {message}")
        return envelope.get("data")

    def current_user(self) -> dict[str, Any]:
        user = self.request("GET", "/api/user/current")
        if user is None:
            raise BBSGoError("token 无效或已过期")
        return user

    def categories(self, topic_type: int = 0) -> list[dict[str, Any]]:
        return self.request(
            "GET",
            "/api/topic/categories",
            params={"type": topic_type},
        )

    def create_topic(
        self,
        category_id: int,
        title: str,
        content: str,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            "/api/topic/create",
            json={
                "type": 0,
                "categoryId": category_id,
                "title": title,
                "contentType": "markdown",
                "content": content,
                "tags": tags or [],
            },
        )

    def create_comment(
        self,
        topic_id: str,
        content: str,
        image_urls: list[str] | None = None,
    ) -> dict[str, Any]:
        image_list = [{"url": url} for url in image_urls or []]
        return self.request(
            "POST",
            "/api/comment/create",
            data={
                "entityType": "topic",
                "entityId": topic_id,
                "content": content,
                "imageList": json.dumps(image_list, ensure_ascii=False),
            },
        )

    def reply_comment(
        self,
        parent_comment_id: int,
        content: str,
        quote_id: int = 0,
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            "/api/comment/create",
            data={
                "entityType": "comment",
                "entityId": str(parent_comment_id),
                "quoteId": quote_id,
                "content": content,
            },
        )


def main() -> None:
    base_url = os.environ["BBS_BASE_URL"]
    token = os.environ["BBS_TOKEN"]
    category_id = int(os.environ["BBS_CATEGORY_ID"])

    client = BBSGoClient(base_url, token)
    user = client.current_user()
    print(f"当前用户: {user.get('nickname') or user.get('username')}")

    topic = client.create_topic(
        category_id=category_id,
        title="脚本发布的帖子",
        content="## 正文\n\n由维护脚本发布。",
        tags=["自动化"],
    )
    topic_id = topic["id"]
    print(f"帖子 ID: {topic_id}, status: {topic.get('status')}")

    comment = client.create_comment(topic_id, "首条自动评论")
    print(f"评论 ID: {comment['id']}")


if __name__ == "__main__":
    main()
```

运行：

```bash
export BBS_BASE_URL='https://bbs.example.com'
read -r -s -p 'BBS token: ' BBS_TOKEN
printf '\n'
export BBS_TOKEN
export BBS_CATEGORY_ID='1'
python3 automation.py
```

生产任务应优先由运行环境的密钥管理服务注入 `BBS_TOKEN`。上述隐藏输入只用于交互式
运行，避免把 token 明文写进 Shell 历史。

当 `topicCaptcha=true` 时，上面的 `create_topic` 还必须接收并发送即时获取、人工完成的验证码字段。

## 9. 常用错误码

| `errorCode` | 含义 | 建议处理 |
| ---: | --- | --- |
| `-1` | 站点未安装 | 停止任务并检查部署状态 |
| `1` | 未登录 | 验证或重新获取 token |
| `2` | 无权限 | 停止任务，不要重试 |
| `1000` | 验证码错误 | 获取新验证码并人工完成 |
| `1001` | 用户被禁言 | 停止发布任务 |
| `1002` | 用户已禁用 | 停止任务并联系管理员 |
| `1003` | 新用户观察期 | 等待响应消息指出的时长后再尝试 |
| `1004` | 邮箱未验证 | 先完成邮箱验证 |

参数错误、分类不匹配、模块关闭、密码错误等情况常使用 `errorCode=0`，仍应以 `success` 和 `message` 为准。

## 10. 自动化实现注意事项

1. 创建帖子和评论没有幂等键。请求超时并不代表服务端没有成功，不能立即盲目重试；应保存本地任务 ID，并通过列表查询或内容指纹去重。
2. 只对可安全重复的 `GET` 请求做自动重试。对创建、编辑、删除等写请求，在确认服务端结果前不要自动重放。
3. 启动任务时先调用 `/api/user/current`；只有 `success=true` 且 `data` 非空才继续。
4. 每次运行先匿名读取登录配置；取得 token 后再次读取完整配置和分类。不要把站点开关、分类 ID、验证码策略或 token 有效期写死。
5. 帖子 ID 按字符串保存，评论 ID 按整数保存。不要把评论响应中的内部 `entityId` 当作外部帖子 ID。
6. 设置连接和读取超时，记录 `errorCode`、`message`、本地任务 ID 与服务端返回 ID，但不要记录 token、密码或验证码。
7. 限制并发和发布频率。当前代码没有启用发帖频率策略，但这不代表调用方可以无限并发；数据库、搜索索引、通知和任务事件仍有写入成本。
8. 内容命中违禁词时可能返回创建成功但 `status=2`。脚本应把它记录为“待审核”，不能当作公开发布成功。

## 11. 源码索引

接口发生变化时，优先核对以下文件：

- 路由：`internal/server/router.go`
- 登录与验证码：`internal/handlers/api/login_handlers.go`、`internal/handlers/api/captcha_handlers.go`
- token 解析：`internal/services/user_token_service.go`
- 全站登录限制：`internal/middleware/login_required_middleware.go`
- 发帖处理器：`internal/handlers/api/topic_handlers.go`
- 发帖请求与校验：`internal/models/req/request.go`、`internal/services/topic_publish_service.go`
- 评论处理器与服务：`internal/handlers/api/comment_handlers.go`、`internal/services/comment_service.go`
- 统一响应：`internal/pkg/ginx/response.go`
- 返回结构：`internal/models/resp/response.go`
- 前端实际调用：`web/lib/api/client.ts`、`web/components/topic/topic-create-form.tsx`、`web/components/comment/index.tsx`
