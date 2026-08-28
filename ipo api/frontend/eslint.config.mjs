/**
 * ESLint 9 flat config.
 *
 * `next lint` was removed in Next 16, so the `lint` script calls ESLint directly.
 * `eslint-config-next` and its subpaths export flat-config arrays, which is why they
 * spread rather than compose through `FlatCompat`.
 *
 * TypeScript already covers types; what earns its keep here is the React and Next
 * layer that a type checker cannot see — stale hook dependencies, a client-only hook
 * in a server component, an `<a>` where a `<Link>` belongs.
 */
import next from "eslint-config-next/core-web-vitals";
import nextTypescript from "eslint-config-next/typescript";

export default [
  { ignores: [".next/**", "node_modules/**", "next-env.d.ts"] },
  ...next,
  ...nextTypescript,
  {
    rules: {
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
    },
  },
  {
    // A flat config *is* an anonymous array by design; the rule is aimed at
    // modules, not at this file.
    files: ["eslint.config.mjs"],
    rules: { "import/no-anonymous-default-export": "off" },
  },
];
