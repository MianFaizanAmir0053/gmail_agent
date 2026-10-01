import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { databaseSsl } from "./tls.ts";

const SUPABASE =
  "postgresql://web_reader.abcdefgh:secret@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres";
const CA = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n";

describe("databaseSsl", () => {
  it("leaves a database on this machine alone", () => {
    for (const url of [
      "postgresql://mailagent:mailagent@localhost:5432/mailagent",
      "postgresql://mailagent:mailagent@127.0.0.1:5432/mailagent",
      "postgresql://mailagent:mailagent@[::1]:5432/mailagent",
    ]) {
      assert.equal(databaseSsl(url, undefined), false, url);
    }
  });

  it("verifies Supabase's certificate when its CA is given", () => {
    const ssl = databaseSsl(SUPABASE, CA);
    assert.ok(ssl);
    assert.equal(ssl.ca, CA);
    assert.notEqual(ssl.rejectUnauthorized, false);
  });

  it("accepts a CA pasted with its line breaks escaped", () => {
    const ssl = databaseSsl(SUPABASE, CA.replace(/\n/g, "\\n"));
    assert.ok(ssl);
    assert.equal(ssl.ca, CA);
  });

  it("still encrypts without the CA", () => {
    // Supabase accepts plaintext unless told otherwise, and so does pg.
    assert.deepEqual(databaseSsl(SUPABASE, undefined), { rejectUnauthorized: false });
    assert.deepEqual(databaseSsl(SUPABASE, "  "), { rejectUnauthorized: false });
  });

  it("encrypts when the address cannot be read", () => {
    assert.deepEqual(databaseSsl("not a url", undefined), { rejectUnauthorized: false });
  });
});
