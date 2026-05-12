import { defineConfig } from "vitest/config";

export default defineConfig({
  resolve: {
    conditions: ["development", "browser"],
  },
  test: {
    environment: "happy-dom",
    include: ["tests/unit/**/*.test.ts", "tests/unit/**/*.test.tsx"],
    globals: false,
    testTimeout: 5_000,
    hookTimeout: 5_000,
    reporters: ["default"],
  },
});
