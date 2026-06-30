import { describe, expect, it } from "vitest";

import { cloudColor, modelColor } from "../lib/colors";

describe("cloudColor", () => {
  it("maps known clouds to their token colors", () => {
    expect(cloudColor("AWS")).toBe("var(--amber)");
    expect(cloudColor("Azure")).toBe("var(--cyan)");
    expect(cloudColor("GCP")).toBe("var(--red)");
    expect(cloudColor("Kubernetes")).toBe("var(--accent-2)");
    expect(cloudColor("VMware")).toBe("var(--green)");
  });

  it("falls back to accent-2 for unknown clouds", () => {
    expect(cloudColor("Oracle")).toBe("var(--accent-2)");
  });
});

describe("modelColor", () => {
  it("maps model families to their token colors", () => {
    expect(modelColor("Claude Sonnet 4.5")).toBe("var(--amber)");
    expect(modelColor("GPT-4o")).toBe("var(--green)");
    expect(modelColor("Gemini 2.5 Pro")).toBe("var(--cyan)");
    expect(modelColor("Azure OpenAI")).toBe("var(--green)"); // OpenAI matches before Azure
    expect(modelColor("Llama 3.1 70B")).toBe("var(--violet)");
  });
});
