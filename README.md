# 分镜生成任务契约

本仓库约定分镜资产中枢与生成供应商之间的任务、回调和文件声明格式。一次生成尝试绑定冻结的输入摘要，供应商扩展信息会被保留供后续核查。

样例位于 `fixtures/callbacks.json`，基础校验由 `src/asset_hub/contracts.py` 提供。执行 `python -m unittest discover -s tests -v` 可检查样例。

项目使用 Python 3.11 以上版本。时间必须带 UTC 偏移，摘要采用小写十六进制 SHA-256，真实媒体文件和访问密钥不应提交。
