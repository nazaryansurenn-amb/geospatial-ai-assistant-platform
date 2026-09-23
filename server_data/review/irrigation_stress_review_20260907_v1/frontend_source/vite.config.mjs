import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";
export default defineConfig({plugins:[react()], publicDir:false, build:{outDir:fileURLToPath(new URL("../../../../output/frontend_irrigation_stress_review_20260907_v1", import.meta.url)), emptyOutDir:false, rollupOptions:{output:{manualChunks:{"react-vendor":["react","react-dom"],"map-engine":["maplibre-gl"]}}}}});
