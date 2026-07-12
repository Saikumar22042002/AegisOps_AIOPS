// U4 — "Auto (ask me)" is the default cloud and maps to cloud=null on the wire, so an ambiguous
// request triggers the backend clarifying question instead of silently defaulting to AWS.
import { describe, expect, it } from "vitest";

import { AUTO_CLOUD, cloudOptions, cloudToWire } from "../lib/data";

describe("U4 cloud selector", () => {
  it("offers Auto as the first option", () => {
    expect(cloudOptions[0].label).toBe(AUTO_CLOUD);
  });

  it("maps Auto to null on the wire", () => {
    expect(cloudToWire(AUTO_CLOUD)).toBeNull();
    expect(cloudToWire("")).toBeNull();
  });

  it("sends a real cloud selection verbatim", () => {
    expect(cloudToWire("AWS")).toBe("AWS");
    expect(cloudToWire("Azure")).toBe("Azure");
    expect(cloudToWire("GCP")).toBe("GCP");
  });
});
