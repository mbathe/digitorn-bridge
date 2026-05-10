/// <reference types="vite/client" />

declare module "*?raw" {
  const content: string;
  export default content;
}

declare module "*?digitorn-seed" {
  const seed: import("@digitorn/preview-sdk").TemplateSeed;
  export default seed;
}
