# 回滚操作手册

## 何时立即回滚

出现以下任一情况，不再继续试运行：

- 店铺名称、Edge Profile 或页面身份无法精确确认；
- 消息平台命中错误群、发送后无法回读或证据缺失；
- 旺店通字段错位、订单识别错误或组件坐标漂移；
- 钉钉文档/工作表/列定位错误或发生重复登记；
- 千牛与拼多多窗口身份混淆；
- 数据库完整性失败、任务数量异常、路径未重写；
- 源电脑与新电脑出现并行执行；
- 出现无法证明结果的退款、驳回、拦截或消息动作。

## 紧急止损顺序

1. 在新电脑立即设置全局暂停并停止运行控制器；
2. 停止 8876 守护进程，确认后台 Python/Edge 工作进程不再继续任务；
3. 不要删除新电脑数据库、日志或证据，先保留现场；
4. 记录最后一个任务、最后动作时间、店铺、订单号和错误截图；
5. 确认新电脑不再产生 `rpa.db`、`backend.log` 或证据文件写入；
6. 只有确认新电脑完全停止后，才允许恢复源电脑。

## 恢复源电脑

1. 检查源电脑项目和数据库仍是切换前保留状态；
2. 确认源电脑 Profile、桌面软件登录和屏幕环境未变化；
3. 先保持源电脑全局暂停，运行只读状态检查；
4. 核对切换窗口内新电脑是否已经产生业务动作，必要时先人工对账；
5. 确认不存在重复退款、重复消息、重复拦截和重复钉钉登记；
6. 由负责人批准后解除源电脑暂停；
7. 人工监控一整轮并记录恢复时间。

## 新电脑数据库恢复点

安装器在重写路径前会生成：

```text
project\runtime\migration_backups\rpa.before_machine_rewrite.<时间>.db
```

迁移包还保留打包时的原始一致性快照：

```text
machine_bound_backup\database\rpa.source_snapshot.db
```

不要直接用文件复制覆盖正在打开的 SQLite 数据库。需要恢复时先停止所有相关进程，保留当前故障库副本，再使用 SQLite 在线备份或在完全关闭连接后替换，并重新执行完整性检查与全局暂停检查。

## 回滚后的排查材料

- `project/runtime/migration_preflight.json`
- `project/runtime/migration_machine_ready.json`
- `project/runtime/backend.log`
- `project/runtime/console_supervisor.log`
- `project/runtime/evidence/`
- `diagnostics/runtime_logs/`
- `MIGRATION_MANIFEST.json`
- `SHA256SUMS.txt`

修复后必须重新从“文件完整性校验”开始走完整验收，不得只重试失败步骤后直接解除暂停。
