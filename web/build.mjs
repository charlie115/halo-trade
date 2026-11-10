// Bundles the page into dist/. Every asset is content-hashed, no script is loaded at runtime
// from outside this site.
import { build } from "esbuild";
import { mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(fileURLToPath(import.meta.url));
const outdir = resolve(root, "dist");
mkdirSync(outdir, { recursive: true });
// Empty rather than replace dist/, so a server bind-mounting it keeps seeing the new files.
for (const entry of readdirSync(outdir)) rmSync(resolve(outdir, entry), { recursive: true, force: true });
mkdirSync(resolve(outdir, "assets"), { recursive: true });

// Pinned at build time so the page can refuse a tampered /onboard/config. Empty means "not pinned",
// which the page accepts on testnet and refuses on mainnet.
const expected = {
  testnet: { grantee: pinned("HALO_EXPECTED_GRANTEE_TESTNET", /^inj1[0-9a-z]{30,60}$/), marketId: pinned("HALO_EXPECTED_MARKET_ID_TESTNET", /^0x[0-9a-f]{64}$/) },
  mainnet: { grantee: pinned("HALO_EXPECTED_GRANTEE_MAINNET", /^inj1[0-9a-z]{30,60}$/), marketId: pinned("HALO_EXPECTED_MARKET_ID_MAINNET", /^0x[0-9a-f]{64}$/) },
};

function pinned(name, pattern) {
  const value = process.env[name] || "";
  if (value && !pattern.test(value)) throw new Error(`${name} is not a valid value`);
  return value;
}

const result = await build({
  absWorkingDir: root,
  entryPoints: ["src/main.js", "src/stats.js", "src/app.css"],
  outdir: "dist/assets",
  bundle: true,
  minify: true,
  splitting: true,
  format: "esm",
  platform: "browser",
  target: "es2022",
  entryNames: "[name]-[hash]",
  chunkNames: "chunk-[hash]",
  assetNames: "[name]-[hash]",
  publicPath: "/assets",
  loader: { ".woff2": "file", ".svg": "file" },
  define: { global: "globalThis", __HALO_EXPECTED__: JSON.stringify(expected) },
  metafile: true,
  legalComments: "none",
  logLevel: "warning",
});

const names = {};
for (const [path, meta] of Object.entries(result.metafile.outputs)) {
  if (!meta.entryPoint) continue;
  const file = path.split("/").pop();
  if (meta.entryPoint.endsWith("main.js")) names.MAIN_JS = `/assets/${file}`;
  if (meta.entryPoint.endsWith("stats.js")) names.STATS_JS = `/assets/${file}`;
  if (meta.entryPoint.endsWith("app.css")) names.CSS = `/assets/${file}`;
}

// The theme script is a classic script in <head>, so it is built apart from the module graph.
const theme = await build({
  absWorkingDir: root,
  entryPoints: ["src/theme.js"],
  outdir: "dist/assets",
  bundle: true,
  minify: true,
  format: "iife",
  platform: "browser",
  target: "es2022",
  entryNames: "[name]-[hash]",
  metafile: true,
  legalComments: "none",
  logLevel: "warning",
});
names.THEME_JS = `/assets/${Object.keys(theme.metafile.outputs)[0].split("/").pop()}`;

names.FAVICON = `data:image/svg+xml;base64,${readFileSync(resolve(root, "src/assets/mark.svg")).toString("base64")}`;

for (const page of ["index.html", "stats.html"]) {
  let html = readFileSync(resolve(root, "src", page), "utf-8");
  for (const [key, value] of Object.entries(names)) html = html.replaceAll(`{{${key}}}`, value);
  writeFileSync(resolve(outdir, page), html);
}
console.log(JSON.stringify(names));
