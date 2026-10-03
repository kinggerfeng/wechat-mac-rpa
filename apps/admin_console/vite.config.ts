import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import { fileURLToPath, URL } from "node:url";

/**
 * The operations console. A separate app from the desktop shell on purpose:
 * its pages read `data/cases.db` and are about judging what the bot already
 * did, which is work an end user of the RPA product never does and should
 * never be shown a link to.
 *
 * The port differs from the desktop dev server so both can run at once during
 * development; the backend port still comes from the one declaration.
 */
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
      "@shared": fileURLToPath(new URL("../shared", import.meta.url)),
    },
  },
  server: {
    port: 1421,
    strictPort: true,
    host: "127.0.0.1",
  },
  clearScreen: false,
});
