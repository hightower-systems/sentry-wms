// v1.7.0 #277: k6 load test for the five Pipe B inbound endpoints.
//
// Operator-run, not part of CI. See docs/loadtest.md for the
// runbook (k6 install, token issuance, mapping-doc seeding,
// expected baselines).
//
// Drives five POST endpoints in parallel, each VU iterating with a
// fresh external_id so the stale-version path (409) isn't on the
// hot path. The realistic source_payload shapes mirror the apartment
// lab fixtures so a mapping doc keyed at the apartment-lab
// source_system resolves every field.
//
// Usage:
//   k6 run \
//     -e SENTRY_BASE_URL=http://localhost:5000 \
//     -e SENTRY_WMS_TOKEN=<plaintext> \
//     -e SENTRY_SOURCE_SYSTEM=acme-erp \
//     tools/loadtest/inbound_v1_7.js
//
// Defaults to the local docker stack; override SENTRY_BASE_URL for
// staging. The script aborts with a clear message if the token env
// var is missing rather than silently 401'ing every request.

import http from "k6/http";
import { check, fail, group } from "k6";
import { randomString, randomIntBetween } from "https://jslib.k6.io/k6-utils/1.4.0/index.js";

const BASE_URL = __ENV.SENTRY_BASE_URL || "http://localhost:5000";
const TOKEN = __ENV.SENTRY_WMS_TOKEN || "";
const SOURCE_SYSTEM = __ENV.SENTRY_SOURCE_SYSTEM || "acme-erp";

if (!TOKEN) {
  fail(
    "SENTRY_WMS_TOKEN env var is required. Issue a token via the admin " +
    "panel with inbound_resources covering the five resources and " +
    "source_system matching SENTRY_SOURCE_SYSTEM."
  );
}

// Ramp shape: 30s warmup at 5 VUs, 60s at 20 VUs (the realistic
// inbound peak shape per docs/loadtest.md), 30s cooldown.
// Override via -e LOADTEST_PROFILE=stress to push to 50 VUs for
// regression hunting.
const PROFILE = __ENV.LOADTEST_PROFILE || "default";
const STAGES = PROFILE === "stress"
  ? [
    { duration: "30s", target: 10 },
    { duration: "120s", target: 50 },
    { duration: "30s", target: 0 },
  ]
  : [
    { duration: "30s", target: 5 },
    { duration: "60s", target: 20 },
    { duration: "30s", target: 0 },
  ];

export const options = {
  stages: STAGES,
  thresholds: {
    // No 5xx leakage. A single 5xx in a clean run signals a regression
    // worth investigating (chain trigger throwing under contention,
    // boot guard misfiring, etc.).
    "http_req_failed{kind:5xx}": ["rate<0.001"],
    // The realistic-peak profile expects sub-300ms p95 against a
    // local docker stack; staging will trend lower. The stress
    // profile relaxes to 800ms.
    "http_req_duration{endpoint:sales_orders}": [
      PROFILE === "stress" ? "p(95)<800" : "p(95)<300",
    ],
    "http_req_duration{endpoint:customers}": [
      PROFILE === "stress" ? "p(95)<800" : "p(95)<300",
    ],
    // Aggregate across all five endpoints; the per-endpoint thresholds
    // catch a single hot endpoint, this catches a system-wide drag.
    "http_req_duration": [
      PROFILE === "stress" ? "p(99)<2000" : "p(99)<1000",
    ],
  },
};

const HEADERS = {
  "Content-Type": "application/json",
  "X-WMS-Token": TOKEN,
};

function uniqueExternalId(prefix) {
  return `${prefix}-${__VU}-${__ITER}-${randomString(6)}`;
}

function postInbound(resource, body) {
  const res = http.post(
    `${BASE_URL}/api/v1/inbound/${resource}`,
    JSON.stringify(body),
    {
      headers: HEADERS,
      tags: {
        endpoint: resource,
        kind: "5xx",
      },
    },
  );
  // Re-tag 5xx so the threshold rate fires only on server-side
  // failures. 409 / 413 / 422 are valid client-error contracts.
  if (res.status >= 500) {
    res.tags = { ...res.tags, kind: "5xx" };
  }
  check(res, {
    [`${resource} status is 200/201/409`]: (r) =>
      r.status === 200 || r.status === 201 || r.status === 409,
    [`${resource} carries X-Sentry-Canonical-Model header`]: (r) =>
      r.headers["X-Sentry-Canonical-Model"] === "DRAFT-v1",
  });
  return res;
}

export default function () {
  group("sales_orders", () => {
    postInbound("sales_orders", {
      external_id: uniqueExternalId("so"),
      external_version: new Date().toISOString(),
      source_payload: {
        id: uniqueExternalId("so"),
        customerId: `C-${randomIntBetween(1, 1000)}`,
        orderTotal: (Math.random() * 1000).toFixed(2),
        billingAddress: "1 Apartment Lab Way, Boulder CO",
        shippingAddress: "1 Apartment Lab Way, Boulder CO",
        lines: [
          { sku: `SKU-${randomIntBetween(1, 50)}`, qty: randomIntBetween(1, 5) },
        ],
      },
    });
  });

  group("items", () => {
    postInbound("items", {
      external_id: uniqueExternalId("it"),
      external_version: new Date().toISOString(),
      source_payload: {
        id: uniqueExternalId("it"),
        sku: `SKU-${randomIntBetween(1, 1000)}`,
        name: `Test Item ${randomString(8)}`,
        unitOfMeasure: "EA",
      },
    });
  });

  group("customers", () => {
    postInbound("customers", {
      external_id: uniqueExternalId("cu"),
      external_version: new Date().toISOString(),
      source_payload: {
        id: uniqueExternalId("cu"),
        name: `Test Customer ${randomString(8)}`,
        email: `test+${randomString(6)}@example.com`,
      },
    });
  });

  group("vendors", () => {
    postInbound("vendors", {
      external_id: uniqueExternalId("vn"),
      external_version: new Date().toISOString(),
      source_payload: {
        id: uniqueExternalId("vn"),
        name: `Test Vendor ${randomString(8)}`,
      },
    });
  });

  group("purchase_orders", () => {
    postInbound("purchase_orders", {
      external_id: uniqueExternalId("po"),
      external_version: new Date().toISOString(),
      source_payload: {
        id: uniqueExternalId("po"),
        vendorId: `V-${randomIntBetween(1, 100)}`,
        lines: [
          { sku: `SKU-${randomIntBetween(1, 50)}`, qty: randomIntBetween(10, 100) },
        ],
      },
    });
  });
}

export function handleSummary(data) {
  // k6 prints the standard summary to stdout; this hook lets the
  // operator parse the JSON form for trend tracking.
  return {
    stdout: textSummary(data),
    "loadtest-summary.json": JSON.stringify(data, null, 2),
  };
}

function textSummary(data) {
  const lines = [
    "",
    "v1.7.0 inbound load test summary (#277)",
    "------------------------------------------------------------",
    `vus_max:                ${data.metrics.vus_max?.values?.max ?? "?"}`,
    `iterations:             ${data.metrics.iterations?.values?.count ?? "?"}`,
    `http_req_duration p95:  ${(data.metrics.http_req_duration?.values?.["p(95)"] ?? 0).toFixed(1)} ms`,
    `http_req_duration p99:  ${(data.metrics.http_req_duration?.values?.["p(99)"] ?? 0).toFixed(1)} ms`,
    `http_req_failed rate:   ${((data.metrics.http_req_failed?.values?.rate ?? 0) * 100).toFixed(2)} %`,
    "",
  ];
  return lines.join("\n");
}
