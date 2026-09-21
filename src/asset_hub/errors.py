"""资产中枢业务异常。"""


class HubError(Exception):
    """违反资产中枢约束（版本、状态机、治理规则）时抛出。"""
