import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { failureKind, flyConfig } from "./flyConfig.ts";

describe("flyConfig", () => {
  it("trims a value pasted with a line break or spaces", () => {
    // A secret ending in a line break makes every request fail: `fetch`
    // refuses such a header, and the log says only TypeError.
    assert.deepEqual(
      flyConfig({ FLY_API_URL: " https://agent.example.ts.net\n", WEB_API_SECRET: "s3cret\r\n" }),
      { base: "https://agent.example.ts.net", secret: "s3cret" },
    );
  });

  it("is null when either is missing or blank", () => {
    assert.equal(flyConfig({ FLY_API_URL: "https://a.example", WEB_API_SECRET: "  " }), null);
    assert.equal(flyConfig({ WEB_API_SECRET: "s3cret" }), null);
  });
});

describe("failureKind", () => {
  it("names the network layer's code, which says why", () => {
    const error = new TypeError("fetch failed", { cause: Object.assign(new Error("x"), { code: "ECONNREFUSED" }) });
    assert.equal(failureKind(error), "TypeError (ECONNREFUSED)");
  });

  it("names an error's own code", () => {
    assert.equal(failureKind(Object.assign(new TypeError("Invalid URL"), { code: "ERR_INVALID_URL" })), "TypeError (ERR_INVALID_URL)");
  });

  it("never logs the message, which can carry the address", () => {
    const kind = failureKind(new TypeError("Failed to parse URL from https://secret.example/api"));
    assert.equal(kind, "TypeError");
  });

  it("copes with anything thrown", () => {
    assert.equal(failureKind("boom"), "unknown error");
  });
});
