"""身份归并业务的用户可读错误。"""


class IdentityError(Exception):
    """可直接展示给页面用户的错误；整批拒绝时 code 说明原因类别。"""

    def __init__(self, message, code="invalid"):
        super().__init__(message)
        self.code = code
