import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { cpSync, existsSync, mkdirSync } from "node:fs";
import { dirname, resolve, relative, isAbsolute } from "node:path";
import { fileURLToPath } from "node:url";

const productRoot = fileURLToPath(new URL(".", import.meta.url));
const outputDirectory = process.env.WORKING_PRODUCT_DIST || "output/frontend_draft";
const analyticsDeliverySlug =
  process.env.LAND_ANALYTICS_DELIVERY_SLUG || "v2_2026_09_05";
const publicAssetAllowlist = [
  "data/cadastre",
  "data/cadastre_overview_low.png",
  "data/cadastre_overview.png",
  "data/communities.geojson",
  "data/community_labels.geojson",
  "data/echmiadzin_wua_boundary.geojson",
  `data/land_analytics/${analyticsDeliverySlug}`,
  "data/lower_hrazdan.geojson",
  "data/lower_hrazdan_halos.geojson",
  "data/lower_hrazdan_labels.geojson",
  "data/lower_hrazdan_points.geojson",
];

function copyApprovedPublicAssets() {
  return {
    name: "copy-approved-public-assets",
    closeBundle() {
      const publicRoot = resolve(productRoot, "public");
      const outputRoot = resolve(productRoot, outputDirectory);

      for (const relativePath of publicAssetAllowlist) {
        const source = resolve(publicRoot, relativePath);
        const destination = resolve(outputRoot, relativePath);
        if (!existsSync(source)) {
          throw new Error(`Missing allowlisted public asset: ${relativePath}`);
        }
        mkdirSync(dirname(destination), { recursive: true });
        cpSync(source, destination, { recursive: true });
      }
    },
  };
}

export default defineConfig(({ command }) => {
  const output = resolve(productRoot, outputDirectory);
  const local = relative(productRoot, output);
  if (!local || local.startsWith("..") || isAbsolute(local)) {
    throw new Error("Build output must stay inside WORKING_PRODUCT.");
  }
  if (!/^[a-z0-9][a-z0-9_-]{2,79}$/.test(analyticsDeliverySlug)) {
    throw new Error("Invalid analytical delivery slug.");
  }
  if (command === "build" && existsSync(output)) {
    throw new Error("Build output already exists. Use a new WORKING_PRODUCT_DIST; preserved versions cannot be overwritten.");
  }
  return {
  plugins: [react(), copyApprovedPublicAssets()],
  server: {
    host: "127.0.0.1",
    port: 8524,
    strictPort: true,
  },
  build: {
    outDir: outputDirectory,
    copyPublicDir: false,
    rollupOptions: {
      output: {
        manualChunks: {
          "react-vendor": ["react", "react-dom"],
          "map-engine": ["maplibre-gl"],
        },
      },
    },
  },
  };
});
