// Copies the shared web app (../radar/webapp) into www/ for Capacitor.
// The radar server serves the same files at /app/, so there is one source of truth.
import { cpSync, rmSync, existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const src = resolve(here, "../../radar/webapp");
const dest = resolve(here, "../www");
if (!existsSync(src)) { console.error(`Web app not found at ${src}`); process.exit(1); }
rmSync(dest, { recursive: true, force: true });
cpSync(src, dest, { recursive: true, filter: p => !p.endsWith("sw.js") }); // no service worker inside the native app
console.log(`Copied ${src} -> ${dest}`);
