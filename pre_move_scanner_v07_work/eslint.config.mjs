// Minimal lint for the dependency-free web UI (run: npx eslint web).
const browser = Object.fromEntries(["window", "document", "location", "fetch", "WebSocket", "setTimeout", "clearTimeout",
  "setInterval", "clearInterval", "console", "navigator", "self", "caches"].map(k => [k, "readonly"]));
export default [{
  files: ["web/**/*.js"],
  languageOptions: { ecmaVersion: 2022, sourceType: "module", globals: browser },
  rules: {
    "no-undef": "error", "no-unused-vars": ["error", { args: "none", caughtErrors: "none" }], "no-unreachable": "error",
    "no-dupe-keys": "error", "no-redeclare": "error", "eqeqeq": ["error", "smart"], "no-const-assign": "error",
    "no-self-assign": "error", "no-unsafe-negation": "error", "use-isnan": "error", "valid-typeof": "error",
  },
}];
