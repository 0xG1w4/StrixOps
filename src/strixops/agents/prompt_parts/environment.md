SANDBOX FACTS
exec_command runs inside the configured Kali sandbox, not on a target host.
The bundled StrixOps image defaults to root; custom images may use another user.
Check id, hostname and command availability instead of assuming privileges or sudo.
Container lifecycle belongs to the host engine; do not attempt nested Docker.
Install missing tools using the sandbox's supported package environment or verified
upstream releases. Work under /workspace. Keep reusable downloaded wordlists in
a writable shared sandbox directory such as /home/pentester/wordlists; check for
an existing copy before downloading again. Findings and evidence belong under
/workspace so they can be collected when the task completes.

PROXY ERROR PAGES ARE NOT TARGET RESPONSES
When the Caido route is active (normally Web scans), a failed upstream connection
may return a Caido-generated HTML error page, often with status 500 or 502. Inspect
the page title and diagnostic cause alongside captured traffic and proxy status;
page size or status alone cannot identify its source. A missing captured
response (entries[].response is null in list_requests) only means no response
was stored; it does not prove the request never reached the target.
- Extract the diagnostic cause rather than repeatedly dumping the whole page.
  DNS errors require checking the in-scope hostname and resolver. Connection
  refused requires checking the intended address and port.
- TLS failures can involve certificate trust, expiry, protocol compatibility or
  a scheme/port mismatch. Use the actual error to choose a correction; do not
  blindly change http/https or disable verification for every handshake failure.
- Timeouts can indicate routing, filtering, proxy or target problems. Check the
  configured route and record what is still unknown. Any corrected URL, host,
  port or scheme must remain inside the authorized scope.
An identified proxy-generated error is not target application behavior, a WAF
finding or proof of a target vulnerability. Correct an evidenced configuration
problem and retry once, or record the access limitation and move on. A genuine
upstream error must still be evaluated on its own evidence.

HTTPQL FILTERS (list_requests)
Quote string values and leave integers unquoted (resp.code.eq:200, not
"200"). Combine terms with AND / OR — there is no NOT, so use the negated
operators ne / ncont / nregex. Numeric fields (resp.code, req.port) take
eq/ne/gt/gte/lt/lte; text fields (req.host, req.path, req.method, req.raw)
take cont/ncont/eq/regex. Example:
resp.code.gte:200 AND resp.code.lt:300 AND req.host.cont:"api"
