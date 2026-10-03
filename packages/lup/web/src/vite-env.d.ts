/// <reference types="vite/client" />

/** A file's bytes as base64 text, as `vite.config.ts`'s `base64Files` builds the module. */
declare module "*?base64" {
  const bytes: string;
  export default bytes;
}
