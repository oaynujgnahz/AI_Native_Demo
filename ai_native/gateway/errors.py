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


class CompanyForbiddenError(Exception):
    pass


class RequestValidationError(Exception):
    def __init__(
        self,
        code: str,
        candidates: list[dict[str, str]] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.candidates = list(candidates or [])[:20]
