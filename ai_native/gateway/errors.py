class GatewayAgentError(Exception):
    def __init__(
        self,
        category: str,
        code: str,
        retryable: bool = False,
        message: str | None = None,
    ) -> None:
        self.category = category
        self.code = code
        self.retryable = retryable
        super().__init__(message or code)
