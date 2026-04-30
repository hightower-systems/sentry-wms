// Mirror of api/services/webhook_dispatcher/error_catalog.py for the
// places in the admin UI that need to render description / triage_hint
// without a server round-trip (notably the per-subscription DLQ viewer,
// whose endpoint has not been extended to return catalog fields).
//
// The cross-subscription /admin/webhook-errors endpoint joins the
// server catalog at response time and ships description + triage_hint
// per row, so it does NOT consult this mirror.
//
// Keep in sync with the python module. Adding a new error_kind there
// without updating here lands a "(no description)" placeholder in the
// DLQ tooltip, which is operator-visible but harmless.

export const ERROR_CATALOG = {
  timeout: {
    short_message: 'Consumer endpoint did not respond in time',
    description: 'The consumer\'s webhook endpoint did not respond within the dispatch timeout (default 10s). The dispatcher abandoned the attempt and will retry per the exponential backoff schedule.',
  },
  connection: {
    short_message: 'Could not connect to consumer endpoint',
    description: 'The TCP connection to the consumer\'s endpoint failed before any HTTP exchange. The hostname may be unresolvable, the port may be closed, or a firewall may be dropping the request.',
  },
  tls: {
    short_message: 'TLS handshake failed',
    description: 'The TLS handshake to the consumer\'s endpoint failed. The consumer may be presenting an expired or self-signed certificate, the cipher suites may not overlap, or the consumer\'s hostname may not match its certificate.',
  },
  ssrf_rejected: {
    short_message: 'Delivery URL resolved to a private or internal IP',
    description: 'The dispatcher\'s SSRF guard rejected the delivery_url because its DNS resolution returned a private (RFC1918), loopback, or cloud-IMDS address. The dispatcher refuses to POST to internal targets to defend against DNS rebinding and split-horizon DNS attacks.',
  },
  '4xx': {
    short_message: 'Consumer rejected the request (4xx response)',
    description: 'The consumer\'s endpoint returned a 4xx HTTP response. The consumer rejected the payload, the signature, the headers, or the request shape. The dispatcher does not follow redirects; 3xx responses also classify as 4xx.',
  },
  '5xx': {
    short_message: 'Consumer endpoint returned a server error (5xx)',
    description: 'The consumer\'s endpoint returned a 5xx HTTP response. The consumer accepted the request but failed to process it. The dispatcher will retry per the exponential backoff schedule; sustained 5xx auto-pauses the subscription via the DLQ ceiling.',
  },
  unknown: {
    short_message: 'Delivery failed (unclassified)',
    description: 'The dispatcher could not classify the failure into one of the documented error_kinds. This usually indicates a library exception the classification layer did not recognize.',
  },
};

export function catalogEntry(kind) {
  return ERROR_CATALOG[kind] || ERROR_CATALOG.unknown;
}
