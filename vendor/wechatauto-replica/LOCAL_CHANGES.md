# 本项目的本地改动

上游固定提交为 `fbfb02677f8f5c8d52be166dc2a662a836f1354d`（1.2.5.1）。来源和原始源码压缩包 SHA256 记录在 `CODEX_PROVENANCE.json`；原始 `LICENSE` 保留。

## 运行目录

首次导入前设置 `WECHATAUTO_RUNTIME_DIR`，使布局、UIA gate 和发送节奏缓存写入宿主指定目录。未设置时保留上游默认目录。数据库的 `workdir`、`keys_file` 由宿主明确传入；稳定密钥副本沿用上游 `WECHATAUTO_KEYS_DIR` 配置。

## 受控数据库初始化

`WeChatDB` 增加两个仅限关键字的参数，默认值保留上游行为：

```python
WeChatDB(db_dir=..., account=..., workdir=..., keys_file=...,
         strict_account=True, allow_key_extraction=False)
```

- `strict_account=True` 要求明确指定 `db_dir` 下存在的账号目录，并禁止自动切换账号。
- `allow_key_extraction=False` 使用指定密钥缓存，禁止自动进程取钥；缓存缺少有效密钥时明确失败。宿主显式允许后可用 `True` 重新初始化，扫描结果只对已选账号数据库验证，不调用其它账号自愈或图片 cfg 补取路径。
- 受控模式中新消息分片缺少密钥时明确失败，要求宿主重新执行显式取钥步骤；监听过程不自动扫描进程。
- 受控模式缺钥报错不自动运行进程权限诊断。解密缓存仍写入 `workdir`，查询解密副本使用 SQLite `mode=ro`。

该改动不启用 GUI，不发送消息，也不改变上游 GUI 的 gate 写入行为。宿主需要自行控制 GUI 初始化和发送权限。

## 本地验证

所有 Python 文件已通过 AST 语法解析。受控初始化使用假密钥、假文件元数据完成隔离检查：缓存可用、缺缓存失败、允许取钥、禁止新分片自动取钥、禁止账号切换与禁止进程取钥。验证未导入整个 SDK 包，未访问微信进程或真实数据库。
