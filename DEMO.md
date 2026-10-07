# 个人微信补习咨询 Demo

入口 `bot.py` 已改为没有导入副作用的分阶段命令。原完整版保存在 `legacy_bot.py`，Demo 不导入它，也不启动 WebUI、主动消息、朋友圈、媒体识别、provider 黑名单检查或辅助模型。

仅支持一个账号、一个唯一备注的测试好友、一个孩子和中文文字消息。真实发送使用 Windows 交互桌面，需保持微信登录、桌面解锁；不承诺无人值守运行。收到非文字消息不交给模型。

## 安装

在仓库目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-demo.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

固定上游机器人提交 `798490beb8602e5dc7d02ad4c79459491da86bf9` 和 SDK 提交 `fbfb02677f8f5c8d52be166dc2a662a836f1354d`（1.2.5.1）。SDK 源码、许可证、来源和局部补丁在 `vendor/wechatauto-replica`，必须使用这里的可编辑安装，不能换成未经修补的同版本 PyPI 包。

## 本机配置

运行 `powershell -ExecutionPolicy Bypass -File .\configure_demo.ps1`，或手工创建以下两个文件。文件只保存在本机，`runtime/` 已从 Git 排除。

`runtime/demo-config.json`：

```json
{
  "db_dir": "D:/xwechat_files",
  "account": "完整账号目录名",
  "test_friend": "补习Demo测试"
}
```

`db_dir` 是含账号目录的父目录，`db_dir/account/db_storage` 必须存在。不要使用旧版 `WeChat Files/账号/Msg`。可先执行 `bot.py accounts 数据库父目录` 仅查看候选文件夹；选定后不自动切换账号。备注有零个或多个精确命中都会退出。

`runtime/secrets.json`：

```json
{"DEEPSEEK_API_KEY": "在本机填写你的真实密钥"}
```

也可使用 `DEEPSEEK_API_KEY` 环境变量，优先级高于文件。模型只允许 `deepseek-flash`，仅调用 `https://api.deepseek.com`。字段提取显式关闭 Flash 的思考模式，避免思考链耗尽额度而不生成 JSON；输出上限2048 tokens。咨询时该测试好友的必要文字、已收集字段和上次问题会发给 DeepSeek；不自动导入历史聊天。请使用虚构学生资料测试。

## 按阶段验证

所有命令均使用 `.\.venv\Scripts\python.exe .\bot.py`，不使用旧 `run.py` 或 WebUI。

1. `check`：检查配置、密钥是否存在、提示词和固定依赖，无微信连接。
2. `bootstrap-keys`：只对指定账号显式扫描数据库密钥，写入 `runtime/keys` 和解密副本，不创建 GUI、不发送。后续缺钥会停止，不偷偷重新提取或改换账号。
3. `probe --seconds 60`：建立历史基线后只监听指定聊天。期间请让测试好友发一条虚构消息，再人工从老师号回一条；收到 incoming/outgoing 且没有未知方向才记为通过。日志只显示方向及类型，不输出聊天正文。
4. `model-check`：用虚构四轮咨询调用真实 DeepSeek，验证两科相同成绩的映射，不连接微信。
5. `run --once --seconds 120`：两项前置验证通过后，先核对电脑上已打开指定聊天且输入框为空；然后收到测试好友的新文字，第一条自动回复经数据库回读确认后退出。
6. `run --seconds 600`：开始十轮完整咨询测试。Ctrl+C 停止。

示例：

```powershell
.\.venv\Scripts\python.exe .\bot.py check
.\.venv\Scripts\python.exe .\bot.py bootstrap-keys
.\.venv\Scripts\python.exe .\bot.py probe --seconds 60
.\.venv\Scripts\python.exe .\bot.py model-check
.\.venv\Scripts\python.exe .\bot.py run --once --seconds 120
```

CLI 退出码 0 是阶段通过，2 是配置失败/暂停/探针不满足条件，130 是人工停止。阶段报告保存在 `runtime/stages.json`，配置变更会使旧报告失效。

## 咨询与异常行为

每轮只请求一次 `deepseek-flash`，同时提取年级、科目、每科期末成绩及自然回复候选。程序合并字段并决定准确的下一问或汇总，再校验候选的问题、科目和成绩对应关系；校验不通过时使用程序原句。新一轮咨询采用仅基于新状态的本地回复，避免旧资料被带入。

一次提供多项会全部合并。成绩保留“82分”“大约70”“刚及格”等原话；明确不知道记为未知，不补成零分。多科成绩不明确时追问，纠正答案后更新汇总，完成后的感谢不重复发送总结。价格、排课和效果由老师确认。

完成后再次发“你好，想了解补习”，或明确说“重新开始咨询”，Flash 会识别新一轮咨询，重新收集字段；不会把上一轮年级和分数带入新一轮。纠正成绩、增删科目继续修改当前记录。

新消息身份包含账号、聊天、分片、表和 local_id，使用包含边界的查询和持久去重，不以正文哈希去重。首次运行只处理基线后的消息；正常重启沿用已存水位、字段和去重记录。串行处理，轮询间隔250毫秒，合并约300毫秒内的连续来信。

发送意图先写入本地 SQLite，再执行一次输入/发送动作；回读必须看到当前目标的新本人消息且正文匹配。界面身份、进程账号证据、方向或发送结果不确定时暂停。默认 SDK 的 UIA 初始化可能修改微信可访问性内存开关；这不是纯数据库发送。

首次就绪、粘贴前、Enter前和接受回执前完整核验账号数据库句柄；中间的界面读取及等待回执只检查已绑定进程的创建时间和窗口PID，不以短时缓存跳过写入边界。输入框直接使用精确UIA锚点查找，省去每次短锚点未命中的1秒等待。`turn_timing` 日志输出模型、界面发送和整轮耗时，不含消息正文或密钥。

暂停或发现上次未确认发送时不会自动重发，也没有微信内“恢复”指令。模型/配置错误修复并人工核对后，可运行 `bot.py resume` 解除暂停，再运行 `run` 继续未处理消息；它不发送，有任何未确认发送时拒绝解除暂停。需要重新开始测试时，可在进程停止后将 `runtime/consultation.sqlite3` 及对应 `-wal`、`-shm` 文件一起移到本地备份目录，再启动建立新基线。不要删密钥来解决咨询状态问题。

## 十轮验收记录

至少覆盖逐项问答、一次说全、两科同为80、成绩未知、科目增删、成绩纠正、连续消息、本人回复回声、重启和发送未确认。额外检查其他好友来信零回复、重复备注退出、API 失败暂停。以实际回读和会话结果记录通过；单元测试及程序能启动不代表个人微信真实收发已通过。
