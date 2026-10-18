## 变更类型

- [ ] 缺陷修复 `fix`
- [ ] 新功能 `feat`
- [ ] 重构 `refactor`
- [ ] 性能优化 `perf`
- [ ] 文档 `docs`
- [ ] 测试 `test`
- [ ] 构建/CI `build`
- [ ] 其他：

## 动机与背景

这个 PR 解决什么问题？关联 issue：#（请填写）

## 主要变更

-

## 作者自检清单

- [ ] 本地 `make test` 通过（默认全量离线：mock 驱动，不联网、不需要真实账号；仅当设置 `EZ_TEST_XQ_COOKIES` 时 `test_xq_follower` 会执行一次雪球登录。依赖 pywinauto，需在 Windows 环境运行）
- [ ] `pipenv run lint` / `format` / `sort_imports` / `type_check` 无新增告警
- [ ] 新行为/修复已补充测试（无法测试的请在备注中说明原因）
- [ ] 无凭据/密钥/资金账号信息入库（account.json、account.session、secrets/ 等已由 .gitignore 覆盖）
- [ ] 日志无敏感信息（资金账号、密码、token）
- [ ] 涉及资金路径：已双向校验（入口参数校验 + 出口回报校验）
- [ ] 公开 API 变更已在描述中说明
- [ ] PR 控制在 ~400 行内，拆不了的已说明拆分理由

## 风险与回滚

最坏情况是什么？如何回滚？（无风险可写"无"）

## 给审查者的备注

哪里需要重点看？哪里是拍脑袋决定的？（没有可写"请随意看"）
