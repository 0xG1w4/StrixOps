"""Stable diagnostics; never expose provider messages or credential-bearing URLs."""

MESSAGES = {
    "invalid_request": "The FOFA request is invalid.",
    "disabled": "FOFA is disabled in settings.",
    "not_configured": "Save a FOFA API key before continuing.",
    "unauthorized": "FOFA rejected the account credentials or account permissions.",
    "quota_exceeded": "FOFA account quota is exhausted or the requested size exceeds its allowance.",
    "rate_limited": "FOFA rate limited this request. Try again later.",
    "provider_error": "FOFA could not complete this request.",
    "invalid_response": "FOFA returned an invalid response.",
    "timeout": "The FOFA request exceeded its time limit.",
    "response_too_large": "The FOFA response exceeded the safe response size limit.",
    "storage_unavailable": "FOFA storage is unavailable. Check its permissions or restore a valid backup.",
    "not_found": "The requested FOFA record was not found.",
    "search_running": "Another FOFA search is running. Wait for it to finish.",
    "selection_limit": "Select between 1 and 100 results for a task draft.",
    "unsupported_targets": "Some selected results lack an unambiguous HTTP or HTTPS target.",
    "origin_rejected": "Cross-origin access to FOFA is not permitted.",
    "interrupted": "The search was interrupted. Saved pages remain available; it was not retried.",
}


class FofaError(Exception):
    def __init__(self, code: str, status: int = 422):
        self.code = code if code in MESSAGES else "provider_error"
        self.status = status
        super().__init__(MESSAGES[self.code])

    def public(self) -> dict:
        return {"success": False, "error_code": self.code, "error": str(self)}
