import { build } from "esbuild";

// Independent ESM entries: every asset can be declared in the host's allowlist.
await build({
  entryPoints: ["frontend/customer/index.ts", "frontend/admin/index.ts"],
  outbase: "frontend",
  outdir: "frontend/dist",
  bundle: true,
  splitting: false,
  format: "esm",
  target: "es2022",
  minify: true,
});
