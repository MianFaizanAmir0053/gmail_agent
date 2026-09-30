import type { MetadataRoute } from "next";

/**
 * Served at `/manifest.webmanifest`, outside the sign-in gate: browsers fetch
 * it without cookies, and without it iOS saves a bookmark instead of
 * installing a web app that can receive push.
 */
export default function manifest(): MetadataRoute.Manifest {
  return {
    name: "mailagent",
    short_name: "mailagent",
    description: "Decide what your assistant proposes.",
    start_url: "/",
    display: "standalone",
    background_color: "#0f172a",
    theme_color: "#0f172a",
    icons: [
      { src: "/icons/icon-192.png", sizes: "192x192", type: "image/png" },
      { src: "/icons/icon-512.png", sizes: "512x512", type: "image/png" },
      // The envelope sits inside the maskable safe zone, so one image serves both.
      { src: "/icons/icon-512.png", sizes: "512x512", type: "image/png", purpose: "maskable" },
    ],
  };
}
